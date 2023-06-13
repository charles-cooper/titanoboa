import subprocess
import sys
import time

import pytest
import requests
from eth_account import Account

import boa
from boa.network import NetworkEnv

ANVIL_FORK_PKEYS = [
    "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80",
    "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d",
    "0x5de4111afa1a4b94908f83103eb1f1706367c2e68ca870fc3fb9a804cdab365a",
    "0x7c852118294e51e653712a81e05800f419141751be58f605c371e15141b007a6",
    "0x47e179ec197488593b187f80a00eb0da91f1b9d0b13f8733639f19c30a34926a",
    "0x8b3a350cf5c34c9194ca85829a2df0ec3153be0318b5e2d3348e872092edffba",
    "0x92db14e403b83dfe3df233f83dfa3a0d7096f21ca9b0d6d6b8d88b2b4ec1564e",
    "0x4bbbf85ce3377467afe5d46f804f221813b2bb87f24d81f60f1fcdbf7cbf4356",
    "0xdbda1821b80551c9d65939329250298aa3472ba22feea921c0cf5d620ea67b97",
    "0x2a871d0798f97d79848a013d4936a73bf4cc922c825d33c1cf7073dff6d409c6",
]


ANVIL_URI = "http://localhost:8545"


@pytest.fixture(scope="session")
def accounts():
    return [Account.from_key(pkey) for pkey in ANVIL_FORK_PKEYS]


# run all tests with this forked environment
# XXX: maybe parametrize across anvil, hardhat and geth --dev for
# max coverage across VM implementations?
@pytest.fixture(scope="package", autouse=True)
def networked_env(accounts):
    # anvil_cmd = f"anvil --fork-url {MAINNET_ENDPOINT} --steps-tracing".split(" ")
    anvil_cmd = "anvil --steps-tracing".split(" ")
    anvil = subprocess.Popen(anvil_cmd, stdout=sys.stdout, stderr=sys.stderr)

    try:
        # wait for anvil to come up
        while True:
            try:
                requests.head(ANVIL_URI)
                break
            except requests.exceptions.ConnectionError:
                time.sleep(0.1)
        with boa.swap_env(NetworkEnv(ANVIL_URI)):
            for account in accounts:
                boa.env.add_account(account)
            yield
    finally:
        anvil.terminate()
        try:
            anvil.wait(timeout=10)
        except subprocess.TimeoutExpired:
            anvil.kill()
            anvil.wait(timeout=1)
