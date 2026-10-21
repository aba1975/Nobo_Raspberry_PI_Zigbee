"""
The +/- buttons step by whole degrees, because the hub cannot store anything else.

Found by the owner on the hardware. Both interfaces stepped by 0.5, and the hub
stores set points as whole degrees, so half steps were never reachable -- the
server rounds to nearest and the two buttons behaved quite differently:

    from 18.0, press +  ->  sends 18.5  ->  hub stores 19.0   (a whole degree)
    from 18.0, press -  ->  sends 17.5  ->  hub stores 18.0   (nothing at all)

So the minus button had simply never worked against a real hub, and the plus
button moved twice as far as the interface implied. Confirmed on the hub before
fixing, and the asymmetry is the reason it went unnoticed: pressing + does
*something*, so the control looks alive.

These read the shipped JavaScript rather than exercising a browser: the value
lives in the source, and a test that reads it is enough to stop it drifting back
to 0.5 without anyone noticing.
"""

import os
import re

UI = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app", "static")


def read(*parts):
    with open(os.path.join(UI, *parts), encoding="utf-8") as fh:
        return fh.read()


class TestTheCabinInterface:
    def test_both_steppers_move_a_whole_degree(self):
        source = read("ui", "cabin", "cabin.js")
        calls = re.findall(r"stepZone\([^)]*?\?\s*(-?[\d.]+)\s*:\s*(-?[\d.]+)\)", source)
        assert calls, "the stepper wiring moved -- find it and update this test"
        for up, down in calls:
            assert float(up) == 1.0, f"up step is {up}"
            assert float(down) == -1.0, f"down step is {down}"

    def test_the_current_value_is_rounded_before_stepping(self):
        """
        Otherwise a half degree set from the official app produces another one
        here, and the room ends up somewhere neither app asked for.
        """
        source = read("ui", "cabin", "cabin.js")
        assert re.search(r"Math\.round\(zone\[field\]", source), \
            "stepZone should round the current set point before adding the step"


class TestNoHalfDegreeIsOffered:
    def test_the_interface_does_not_carry_a_half_step(self):
        source = read("ui", "cabin", "cabin.js")
        for match in re.findall(r"stepZone\([^)]*\)", source):
            assert "0.5" not in match, match