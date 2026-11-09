"""Shared broker contract. A fill dict: {px, qty, fee, ts, algo_id?}."""


class BrokerError(Exception):
    pass


def inst_id(asset, side):
    """Longs trade spot, shorts trade the USDT perpetual swap."""
    return f"{asset}-USDT" if side == 1 else f"{asset}-USDT-SWAP"


def make_clid(kind, day, asset, account):
    """OKX client order id: letters/digits only, <= 32 chars. Same inputs -> same id, so a retry
    after a crash can find the earlier order instead of placing a second one."""
    return f"m{account[0]}{kind}{day}{asset}"[:32]
