"""Example runPython target: computes a cosine wave. Stdlib only"""
import math


def main(n: int = 80, freq: float = 1.0) -> dict:
    points = []
    for i in range(n):
        x = i / (n - 1) if n > 1 else 0.0
        points.append([x, math.cos(10 * math.pi * freq * x)])
    return {"points": points}
