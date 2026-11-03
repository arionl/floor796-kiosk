#!/usr/bin/env python3
"""modes.py — the three canonical camera modes.

Design spec (2026-11-01):

  Wandering   self-driving camera (A* tours); the default.  Arrow
              keys are inert here — SPACE enters Navigating for
              manual pan.
  Navigating  user-driven pan; entered by a click from Wandering; a
              click advances to Exploring.
  Exploring   object-to-object navigation (see explorer.py); a click
              returns to Wandering, which resumes the tour from the
              current camera position.

SPACE cycles modes; arrow keys pan (Navigating) or jump
(Exploring).  Pure logic — no pygame, no I/O.
"""

from enum import Enum


class Mode(Enum):
    WANDERING = "wandering"
    NAVIGATING = "navigating"
    EXPLORING = "exploring"


_CYCLE_ORDER = (Mode.WANDERING, Mode.NAVIGATING, Mode.EXPLORING)


class ModeMachine:
    """Select-press (SPACE) cycles Wandering → Navigating → Exploring →
    Wandering.  Arrows are inert in Wandering (the map cannot be
    moved by hand until Navigating).  Resuming
    Wandering continues the tour from the current camera — no
    snap-back.

    Transition table:
      exploring → select → exit exploring + wander;
      wandering → select → stop wandering (= Navigating);
      navigating → select → enter exploring.
    """

    def __init__(self, initial=Mode.WANDERING):
        self._mode = initial

    # ── State ────────────────────────────────────────────────────────

    @property
    def current(self):
        return self._mode

    # ── Transitions ──────────────────────────────────────────────────

    def cycle(self):
        """Select-press: advance one mode (with the exit rules above)."""
        if self._mode is Mode.EXPLORING:
            # Exploring exits directly to Wandering (resume tour).
            self._mode = Mode.WANDERING
        else:
            idx = _CYCLE_ORDER.index(self._mode)
            self._mode = _CYCLE_ORDER[(idx + 1) % len(_CYCLE_ORDER)]
        return self._mode

    def pan_started(self):
        """A pan gesture began (arrow key).

        Arrows only pan in NAVIGATING (in Wandering they are inert —
        SPACE first).  In Exploring a
        pan is not a pan — it is a directional JUMP; the mode is
        unchanged and the caller routes the input to the Explorer.
        This method is mode-preserving; it exists so callers have one
        choke point for pan semantics."""
        return self._mode

    def force(self, mode):
        """External control hook (e.g. the HTTP /viewport endpoint
        pinning the camera): set the mode outright, bypassing the
        click-cycle."""
        self._mode = mode
        return self._mode

    # ── Per-mode behavior flags ─────────────────────────────────────

    @property
    def allows_wander(self):
        """Wanderer may drive the camera (Wandering only)."""
        return self._mode is Mode.WANDERING

    @property
    def allows_free_pan(self):
        """Arrows pan the camera freely (Navigating only; in Wandering
        a pan first switches to Navigating)."""
        return self._mode is Mode.NAVIGATING

    @property
    def explorer_drives_camera(self):
        """Explorer flights own the camera (Exploring only)."""
        return self._mode is Mode.EXPLORING
