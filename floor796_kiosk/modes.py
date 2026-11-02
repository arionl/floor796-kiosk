#!/usr/bin/env python3
"""modes.py — the three canonical camera modes (tvOS parity).

Mirrors floor796-tvos GameViewController's mode semantics
(docs/architecture.md §3.1):

  Wandering   self-driving camera (A* tours); the default.  Arrow
              keys are inert here (kiosk rule) — SPACE enters
              Navigating for manual pan.
  Navigating  user-driven pan; entered by a click from Wandering; a
              click advances to Exploring.
  Exploring   object-to-object navigation (see explorer.py); a click
              returns to Wandering, which resumes the tour from the
              current camera position.

Keyboard stands in for the Siri Remote: SPACE = select-click (cycles
modes), arrow keys = pan/flick.  Pure logic — no pygame, no I/O.
"""

from enum import Enum


class Mode(Enum):
    WANDERING = "wandering"
    NAVIGATING = "navigating"
    EXPLORING = "exploring"


_CYCLE_ORDER = (Mode.WANDERING, Mode.NAVIGATING, Mode.EXPLORING)


class ModeMachine:
    """Select-press (SPACE) cycles Wandering → Navigating → Exploring →
    Wandering.  Arrows are inert in Wandering (kiosk rule: the map
    cannot be moved by hand until Navigating).  Resuming
    Wandering continues the tour from the current camera — no
    snap-back.

    Exactly the tvOS cycleMode() semantics:
      exploring → click → exit exploring + wander;
      wandering → click → stop wandering (= Navigating);
      navigating → click → enter exploring.
    """

    def __init__(self, initial=Mode.WANDERING):
        self._mode = initial

    # ── State ────────────────────────────────────────────────────────

    @property
    def current(self):
        return self._mode

    # ── Transitions ──────────────────────────────────────────────────

    def cycle(self):
        """Select-press: advance one mode (with the tvOS exit rules)."""
        if self._mode is Mode.EXPLORING:
            # Exploring exits directly to Wandering (resume tour).
            self._mode = Mode.WANDERING
        else:
            idx = _CYCLE_ORDER.index(self._mode)
            self._mode = _CYCLE_ORDER[(idx + 1) % len(_CYCLE_ORDER)]
        return self._mode

    def pan_started(self):
        """A pan gesture began (arrow key).

        Arrows only pan in NAVIGATING (kiosk rule, arion: arrows in
        Wandering must not move the map — SPACE first).  In Exploring a
        pan is not a pan — it is a directional JUMP; the mode is
        unchanged and the caller routes the input to the Explorer.
        This method is mode-preserving; it exists so callers have one
        choke point for pan semantics."""
        return self._mode

    def force(self, mode):
        """External control hook (e.g. the HTTP /viewport endpoint
        pinning the camera): set the mode outright, bypassing the
        click-cycle.  Not part of the tvOS remote semantics."""
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
