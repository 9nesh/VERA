"""
Solana attestation layer for VERA.

Builds a compact attestation payload from project flags, SHA-256 hashes it,
and writes the hash + metadata to the SPL Memo program on Solana devnet.
This creates a tamper-proof, timestamped record that any lender can verify
without trusting the VERA backend.

Requirements: solders==0.21.0, solana==0.35.0
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from solders.instruction import AccountMeta, Instruction
from solders.keypair import Keypair
from solders.message import Message
from solders.pubkey import Pubkey
from solders.signature import Signature
from solders.transaction import Transaction
from solana.rpc.async_api import AsyncClient
from solana.rpc.commitment import Confirmed
from solana.rpc.types import TxOpts

# SPL Memo program — deployed on all Solana clusters, no extra deploy needed
_MEMO_PROGRAM_ID = Pubkey.from_string("MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr")

_DEVNET_EXPLORER = "https://explorer.solana.com/tx/{sig}?cluster=devnet"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_keypair(keypair_path: str) -> Keypair:
    """Load a Solana keypair from a JSON file containing an array of 64 bytes."""
    data = json.loads(Path(keypair_path).read_text())
    return Keypair.from_bytes(bytes(data))


def _flag_summary(flags: list[dict]) -> dict[str, int]:
    """Summarize flags by severity: {high: N, medium: N, low: N, info: N}."""
    out: dict[str, int] = {}
    for f in flags:
        sev = f.get("severity", "low")
        out[sev] = out.get(sev, 0) + 1
    return out


def _build_payload(
    project_id: str,
    flags: list[dict],
    doc_hashes: dict[str, str],
) -> tuple[str, str]:
    """
    Build the attestation payload dict, JSON-serialize it, and SHA-256 hash it.
    Returns (payload_json, sha256_hex).
    """
    summary = _flag_summary(flags)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Build the full payload for hashing (includes doc_hashes for integrity)
    full = {
        "v": 1,
        "pid": project_id,
        "ts": ts,
        "flags": summary,
        "doc_hashes": doc_hashes,
    }
    full_json = json.dumps(full, sort_keys=True, separators=(",", ":"))
    sha = hashlib.sha256(full_json.encode("utf-8")).hexdigest()

    # The memo written on-chain: compact, human-readable
    memo_obj = {
        "v": 1,
        "pid": project_id,
        "ts": ts,
        "flags": summary,
        "hash": f"sha256:{sha}",
    }
    memo_json = json.dumps(memo_obj, separators=(",", ":"))
    return memo_json, sha


def _build_memo_tx(memo_text: str, keypair: Keypair, recent_blockhash) -> Transaction:
    """Build an unsigned transaction with a single SPL Memo instruction."""
    ix = Instruction(
        program_id=_MEMO_PROGRAM_ID,
        data=memo_text.encode("utf-8"),
        accounts=[AccountMeta(pubkey=keypair.pubkey(), is_signer=True, is_writable=False)],
    )
    msg = Message.new_with_blockhash(
        [ix],
        keypair.pubkey(),
        recent_blockhash,
    )
    return Transaction([keypair], msg, recent_blockhash)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def attest_project(
    project_id: str,
    flags: list[dict],
    doc_hashes: dict[str, str],
    keypair_path: str,
    rpc_url: str = "https://api.devnet.solana.com",
) -> dict:
    """
    Build attestation payload, hash it, send to Solana via SPL Memo program.

    Returns:
        {tx_signature, slot, payload_hash, explorer_url}
    """
    keypair = _load_keypair(keypair_path)
    memo_json, payload_hash = _build_payload(project_id, flags, doc_hashes)

    # Memo must be ≤ 566 bytes for a single instruction; truncate if somehow over
    memo_bytes = memo_json.encode("utf-8")
    if len(memo_bytes) > 566:
        # Rebuild without doc_hashes to save space
        summary = _flag_summary(flags)
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        short = {"v": 1, "pid": project_id, "ts": ts, "flags": summary, "hash": f"sha256:{payload_hash}"}
        memo_bytes = json.dumps(short, separators=(",", ":")).encode("utf-8")

    async with AsyncClient(rpc_url) as client:
        # Fetch recent blockhash
        bh_resp = await client.get_latest_blockhash(commitment=Confirmed)
        recent_blockhash = bh_resp.value.blockhash
        last_valid_block_height = bh_resp.value.last_valid_block_height

        # Build and send transaction
        ix = Instruction(
            program_id=_MEMO_PROGRAM_ID,
            data=memo_bytes,
            accounts=[AccountMeta(pubkey=keypair.pubkey(), is_signer=True, is_writable=False)],
        )
        msg = Message.new_with_blockhash([ix], keypair.pubkey(), recent_blockhash)
        tx = Transaction([keypair], msg, recent_blockhash)

        opts = TxOpts(skip_confirmation=False, preflight_commitment=Confirmed)
        send_resp = await client.send_transaction(tx, opts=opts)

        if hasattr(send_resp, "value"):
            sig: Signature = send_resp.value
        else:
            raise RuntimeError(f"Solana send_transaction failed: {send_resp}")

        # Confirm the transaction
        await client.confirm_transaction(
            sig,
            commitment=Confirmed,
            sleep_seconds=1.0,
            last_valid_block_height=last_valid_block_height,
        )

        # Fetch the confirmed slot
        slot = None
        try:
            tx_resp = await client.get_transaction(
                sig,
                commitment=Confirmed,
                max_supported_transaction_version=0,
            )
            if tx_resp.value:
                slot = tx_resp.value.slot
        except Exception:
            pass  # slot is non-critical

        sig_str = str(sig)
        return {
            "tx_signature": sig_str,
            "slot": slot,
            "payload_hash": payload_hash,
            "explorer_url": _DEVNET_EXPLORER.format(sig=sig_str),
        }


async def verify_attestation(
    tx_signature: str,
    expected_hash: str,
    rpc_url: str = "https://api.devnet.solana.com",
) -> dict:
    """
    Fetch the on-chain memo for a transaction and verify the payload hash.

    Returns:
        {verified: bool, memo_content: str | None, confirmed_at_slot: int | None}
    """
    sig = Signature.from_string(tx_signature)

    async with AsyncClient(rpc_url) as client:
        tx_resp = await client.get_transaction(
            sig,
            encoding="json",
            commitment=Confirmed,
            max_supported_transaction_version=0,
        )

    if not tx_resp.value:
        return {"verified": False, "memo_content": None, "confirmed_at_slot": None}

    tx_data = tx_resp.value
    slot = getattr(tx_data, "slot", None)

    # Extract memo from the transaction JSON representation
    memo_content: str | None = None
    try:
        tx_json = json.loads(tx_data.to_json())
        # Walk into transaction.message.instructions to find Memo program data
        instructions = (
            tx_json.get("transaction", {})
            .get("message", {})
            .get("instructions", [])
        )
        for ix in instructions:
            # Memo data is stored as base58-encoded string in the "data" field
            raw_data = ix.get("data") or ix.get("parsed")
            if raw_data and isinstance(raw_data, str):
                try:
                    import base58  # type: ignore[import]
                    decoded = base58.b58decode(raw_data).decode("utf-8")
                    if "vera" in decoded.lower() or "pid" in decoded:
                        memo_content = decoded
                        break
                except Exception:
                    # Try UTF-8 direct
                    try:
                        memo_content = raw_data
                        break
                    except Exception:
                        pass
            elif raw_data and isinstance(raw_data, dict):
                # Parsed memo instruction
                memo_content = raw_data.get("info") or str(raw_data)
                break
    except Exception:
        pass

    # Verify hash if memo was recovered
    verified = False
    if memo_content:
        try:
            memo_obj = json.loads(memo_content)
            on_chain_hash = memo_obj.get("hash", "")
            check = f"sha256:{expected_hash}" if not expected_hash.startswith("sha256:") else expected_hash
            verified = on_chain_hash == check
        except Exception:
            # If we can't parse, check if expected_hash appears literally
            verified = expected_hash in memo_content

    return {
        "verified": verified,
        "memo_content": memo_content,
        "confirmed_at_slot": slot,
    }
