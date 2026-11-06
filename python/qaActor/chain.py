"""Own the QA output chain: the actor's RUNs, newest first, then its input collections as chained members.

Run with the stack's python by the `qa` controller (``python -m qaActor.chain``): at start, to define or
repair the chain, and after each visit's pipetask, to prepend the RUN it wrote. pipetask itself never
touches the chain (INSTRM-3096): given ``-o``, it would write the inputs into the chain flattened, as they
were at that run, and build every later quantum graph against that snapshot, so the first new
``drpActor/reductions`` RUN would make every later visit fail. With ``--output-run`` and no ``-o`` the
inputs are resolved when each visit runs, and this module keeps the chain the loader and the judge read.
"""

import argparse
import sys
from collections.abc import Callable, Iterable, Sequence


def desired_chain(
    current: Sequence[str], collection: str, inputs: Iterable[str], run: str | None = None
) -> list[str]:
    """Return the chain's definition: ``run`` first if given, the collection's own RUNs, then the inputs.

    Anything else in ``current`` (the inputs as pipetask flattened them, a stray member) is dropped: the
    inputs enter as the chained collections named, so they follow their own membership.
    """
    own = [member for member in current if member.startswith(f"{collection}/") and member != run]
    return ([run] if run else []) + own + list(inputs)


def define_chain(
    butler, collection: str, inputs: Sequence[str], run: str | None = None, log: Callable = print
) -> bool:
    """Define ``collection`` as `desired_chain` says, registering it if needed; return whether it changed.

    A ``run`` that does not exist (pipetask failed before writing anything) is reported and left out.
    """
    from lsst.daf.butler import CollectionType, MissingCollectionError

    try:
        current = list(butler.collections.get_info(collection).children)
    except MissingCollectionError:
        butler.collections.register(collection, CollectionType.CHAINED)
        current = []
        log(f"{collection}: registered as CHAINED")
    if run is not None:
        try:
            butler.collections.get_info(run)
        except MissingCollectionError:
            log(f"{run}: no such RUN (pipetask wrote nothing), not chained")
            run = None
    desired = desired_chain(current, collection, inputs, run)
    own = sum(member.startswith(f"{collection}/") for member in desired)
    if desired == current:
        log(f"{collection}: unchanged, {own} RUNs then {', '.join(inputs)}")
        return False
    butler.collections.redefine_chain(collection, desired)
    dropped = len(current) - sum(member in desired for member in current)
    what = f"prepended {run}" if run else "repaired"
    log(
        f"{collection}: {what}; {own} RUNs then {', '.join(inputs)}"
        + (f"; {dropped} flattened members dropped" if dropped else "")
    )
    return True


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Define the QA output chain: own RUNs, then the inputs as chained members."
    )
    parser.add_argument("-b", "--butler", required=True, help="Butler repository")
    parser.add_argument("-c", "--collection", required=True, help="the CHAINED output collection")
    parser.add_argument(
        "-i", "--inputs", required=True, help="input collections, comma-separated, in search order"
    )
    parser.add_argument(
        "--run", default=None, help="a RUN of the collection to prepend (a visit's pipetask output)"
    )
    args = parser.parse_args(argv)
    from lsst.daf.butler import Butler

    butler = Butler(args.butler, writeable=True)
    define_chain(
        butler,
        args.collection,
        [name for name in args.inputs.split(",") if name],
        args.run,
        log=lambda line: print(line, flush=True),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
