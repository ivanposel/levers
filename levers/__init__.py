"""levers — one owner for every kill, restart and reboot in your system.

    from levers import Levers, Lever

    box = Levers(state_dir="/var/lib/myapp/levers")
    box.register(Lever("database", run=["systemctl", "restart", "postgresql"],
                       cooldown=900, destroys="db", verify=db_answers,
                       doc="restart the database everything attaches to"))

    result = box.pull("database", why="every connection wedged for 20 minutes",
                      actor="health-loop")
    if result.ok is None:
        ...   # could not tell whether it worked. Not a success, not a failure.
"""

from ._core import Lever, LeverError, Levers, Pull

__all__ = ["Lever", "Levers", "Pull", "LeverError", "__version__"]
__version__ = "0.1.0"
