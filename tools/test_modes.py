#!/usr/bin/env python3
"""Test modes.py — the canonical three-mode state machine (tvOS parity).

Select-press (SPACE) cycles Wandering → Navigating → Exploring →
Wandering; any pan in Wandering enters Navigating; a pan in Exploring
is a jump (mode unchanged); re-entering Wandering resumes the tour.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from floor796_kiosk.modes import Mode, ModeMachine  # noqa: E402


class TestModeMachine(unittest.TestCase):

    def test_default_is_wandering(self):
        self.assertIs(ModeMachine().current, Mode.WANDERING)

    def test_cycle_order(self):
        m = ModeMachine()
        self.assertIs(m.cycle(), Mode.NAVIGATING)
        self.assertIs(m.cycle(), Mode.EXPLORING)
        self.assertIs(m.cycle(), Mode.WANDERING)   # exits exploring → wander
        self.assertIs(m.cycle(), Mode.NAVIGATING)

    def test_exploring_cycles_directly_to_wandering(self):
        m = ModeMachine(Mode.EXPLORING)
        self.assertIs(m.cycle(), Mode.WANDERING)

    def test_pan_in_wandering_enters_navigating(self):
        m = ModeMachine()
        self.assertIs(m.pan_started(), Mode.NAVIGATING)

    def test_pan_in_navigating_stays(self):
        m = ModeMachine(Mode.NAVIGATING)
        self.assertIs(m.pan_started(), Mode.NAVIGATING)

    def test_pan_in_exploring_is_a_jump_not_a_pan(self):
        m = ModeMachine(Mode.EXPLORING)
        self.assertIs(m.pan_started(), Mode.EXPLORING)
        self.assertFalse(m.allows_free_pan)

    def test_flags_per_mode(self):
        m = ModeMachine()
        self.assertTrue(m.allows_wander)
        self.assertFalse(m.allows_free_pan)
        self.assertFalse(m.explorer_drives_camera)

        m.cycle()
        self.assertFalse(m.allows_wander)
        self.assertTrue(m.allows_free_pan)
        self.assertFalse(m.explorer_drives_camera)

        m.cycle()
        self.assertFalse(m.allows_wander)
        self.assertFalse(m.allows_free_pan)
        self.assertTrue(m.explorer_drives_camera)

    def test_wandering_resume_has_no_snapback_semantics(self):
        # The machine carries no stored position — resuming Wandering
        # is the caller syncing wanderer.x/y to the CURRENT camera
        # (player.py does this on every W→N→W round trip).
        m = ModeMachine(Mode.EXPLORING)
        m.cycle()                       # → WANDERING (from exploring)
        self.assertTrue(m.allows_wander)


if __name__ == '__main__':
    unittest.main(verbosity=2)
