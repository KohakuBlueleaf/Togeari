"""Study sizes: ``small`` checks the scripts in seconds; ``full`` answers the questions."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Scale:
    name: str
    # Rounds over the CPU configurations, interleaved
    rounds: int
    # Share of each ceiling shape's jobs that runs
    ceiling_share: float
    # Stations of examples/mixed in study A and in study C
    mixed_stations: int
    batch_stations: int
    # Rounds over the GPU configurations
    gpu_rounds: int


SCALES = {
    "small": Scale(
        "small", rounds=1, ceiling_share=0.1, mixed_stations=12, batch_stations=12, gpu_rounds=1
    ),
    "full": Scale(
        "full", rounds=3, ceiling_share=1.0, mixed_stations=120, batch_stations=480, gpu_rounds=3
    ),
}
