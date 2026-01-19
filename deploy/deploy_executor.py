import json
import os
import shutil
import subprocess
from pathlib import Path

from web3 import Web3


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = PROJECT_ROOT / "contracts" / "ArbitrageExecutor.sol"


def _compile() -> tuple[list, str]:
    solc = shutil.which("solc")
    if not solc:
        raise RuntimeError("solc not found in PATH (install solc to compile contracts)")
    cmd = [
        solc,
        "--optimize",
        "--combined-json",
        "abi,bin",
        str(CONTRACT_PATH),
    ]
    out = subprocess.check_output(cmd, cwd=str(PROJECT_ROOT))
    data = json.loads(out.decode("utf-8"))
    key = None
    for k in data.get("contracts", {}):
        if k.endswith(":ArbitrageExecutor"):
            key = k
            break
    if not key:
        raise RuntimeError("ArbitrageExecutor contract not found in solc output")
    abi = json.loads(data["contracts"][key]["abi"])
    bytecode = data["contracts"][key]["bin"]
    return abi, bytecode


def deploy() -> None:
    rpc_url = os.getenv("RPC_URL")
    private_key = os.getenv("PRIVATE_KEY")
    if not rpc_url or not private_key:
        raise RuntimeError("RPC_URL and PRIVATE_KEY env vars are required")
    abi, bytecode = _compile()
    w3 = Web3(Web3.HTTPProvider(rpc_url))
    acct = w3.eth.account.from_key(private_key)
    chain_id = int(w3.eth.chain_id)
    contract = w3.eth.contract(abi=abi, bytecode=bytecode)
    txn = contract.constructor().build_transaction(
        {
            "from": acct.address,
            "nonce": w3.eth.get_transaction_count(acct.address),
            "chainId": chain_id,
            "gas": 2_500_000,
            "gasPrice": w3.eth.gas_price,
        }
    )
    signed = acct.sign_transaction(txn)
    tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash)
    print(f"Deployed ArbitrageExecutor to {receipt.contractAddress}")


if __name__ == "__main__":
    deploy()
