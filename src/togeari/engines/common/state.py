"""States of the builtin engines (reported in ``load().extra["state"]``)."""


class EngineState:
    STOPPED = "stopped"
    STARTING = "starting"
    UP = "up"
    DOWN = "down"
