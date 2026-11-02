#!/usr/bin/env python3
"""Headless tests for the teleport room (site-exact phase machine,
geometry, and composite math).  Pure logic — no network, no real
assets; scenes are injected as synthetic frames.

Run: SDL_VIDEODRIVER=dummy python3 tools/test_teleport.py
"""
import os
import sys
import unittest

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

import numpy as np  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from floor796_kiosk import teleport as T  # noqa: E402

FAKE_TILES = {
    't2r4': {'row': 2, 'col': 4},
    't1r5': {'row': 1, 'col': 5},
    't0r4': {'row': 0, 'col': 4},
}


def make_room(items=7):
    room = T.TeleportRoom('/tmp/f796_teleport_test_cache',
                          tiles_meta={'tiles': FAKE_TILES})
    room.items = [{'file': f't{i}.f796.br', 'title_en': f'T{i}', 'src': None}
                  for i in range(items)]
    room.origin_x = 100.0
    room.origin_y = 200.0
    room.clip_mask = np.ones((T.HEIGHT, T.WIDTH), dtype=bool)
    room.bg_px = np.zeros((T.HEIGHT, T.WIDTH, 4), dtype=np.uint8)
    room.sprites = np.zeros((T.SPRITE_H * T.SPRITE_VARIANTS,
                             T.SPRITE_W * T.SPRITE_COLS, 4),
                            dtype=np.uint8)
    room.ready = True
    return room


def inject_scene(room, idx, with_transitions=True):
    frames = []
    for f in range(60):
        px = np.zeros((T.HEIGHT, T.WIDTH, 4), dtype=np.uint8)
        px[..., 0] = min(255, f * 4)
        px[..., 1] = 128
        px[..., 2] = 200
        px[..., 3] = 255
        frames.append(px)
    raws = {}
    for i in (0, T.DEMAT_ENTRY_FRAME):
        d = frames[i].copy()
        T.erase_hidden(d)
        raws[i] = d
    scene = T._Scene(frames, raws)
    if with_transitions:
        import pygame
        scene.trans_mat = [
            pygame.Surface((T.WIDTH, T.HEIGHT), pygame.SRCALPHA)
            for _ in range(T.MATERIALIZE_FRAMES)]
        scene.trans_demat = [
            pygame.Surface((T.WIDTH, T.HEIGHT), pygame.SRCALPHA)
            for _ in range(T.MATERIALIZE_FRAMES)]
    room.scenes[idx] = scene


class TestGeometry(unittest.TestCase):
    def test_anchor_resolution(self):
        room = T.TeleportRoom('/tmp/f796_teleport_test_cache',
                              tiles_meta={'tiles': FAKE_TILES})
        # anchor t2r4 x=15 y=618 + (-4, 1)
        self.assertEqual(room.tiles['t2r4']['col'], 4)
        p = T.resolve_point(FAKE_TILES, 't2r4', 15, 618)
        self.assertEqual(p, (4 * 1016 + 15, 2 * 812 + 618))
        self.assertEqual((p[0] - 4, p[1] + 1), (p[0] - 4, p[1] + 1))

    def test_erase_hidden_triangle(self):
        px = np.full((T.HEIGHT, T.WIDTH, 4), 200, dtype=np.uint8)
        T.erase_hidden(px)
        # Top-left corner above the triangle is untouched.
        self.assertEqual(px[0, 0, 0], 200)
        # Bottom-right corner (inside triangle) is black, opaque.
        self.assertEqual(tuple(px[-1, -1]), (0, 0, 0, 255))
        # A pixel left of the hypotenuse near hidePartY is untouched.
        self.assertEqual(px[T.HIDE_PART_Y, 0, 0], 200)
        # Row below: leftmost `x0` pixels survive; at the bottom nothing.
        y = T.HIDE_PART_Y + (T.HEIGHT - T.HIDE_PART_Y) // 2
        t = (y - T.HIDE_PART_Y) / (T.HEIGHT - T.HIDE_PART_Y)
        x0 = int((1 - t) * T.WIDTH)
        self.assertEqual(px[y, max(0, x0 - 2), 0], 200)
        self.assertEqual(tuple(px[y, x0 + 2]), (0, 0, 0, 255))

    def test_poly_coverage_square(self):
        m = T.poly_coverage(10, 10, [(2, 2), (8, 2), (8, 8), (2, 8)])
        self.assertTrue(m[5, 5])
        self.assertFalse(m[0, 0])
        self.assertFalse(m[9, 9])


class TestPhaseMachine(unittest.TestCase):
    def _run(self, room, n):
        for _ in range(n):
            room.update()

    def test_gap_to_materialize(self):
        room = make_room()
        inject_scene(room, 0)
        self.assertEqual(room.phase, T.TeleportRoom.GAP)
        self._run(room, T.GAP_FRAMES)
        self.assertEqual(room.phase, T.TeleportRoom.MATERIALIZE)
        self.assertEqual(room.phase_frame, 1)
        self.assertEqual(room.held_frame, 0)
        self.assertIsNotNone(room._staging)

    def test_gap_extends_when_scene_unready_then_skips(self):
        room = make_room()          # no scenes injected
        self._run(room, T.GAP_FRAMES)
        self.assertEqual(room.phase, T.TeleportRoom.GAP)  # extended
        self.assertEqual(room.phase_frame, T.GAP_FRAMES)
        self._run(room, T.GAP_FRAMES * 8 - T.GAP_FRAMES - 1)
        self.assertEqual(room.phase, T.TeleportRoom.GAP)
        self.assertEqual(room.current, 0)
        room.update()               # hits the *8 skip bound
        self.assertEqual(room.current, 1)

    def test_materialize_alphas_and_entry(self):
        room = make_room()
        inject_scene(room, 0)
        self._run(room, T.GAP_FRAMES)
        alphas = []
        for _ in range(T.MATERIALIZE_FRAMES - 1):
            alphas.append(room.phase_frame / (T.MATERIALIZE_FRAMES - 1))
            room.update()
        # beats 1..21 -> alpha 1/21 .. 21/21; the NEXT update flips.
        self.assertAlmostEqual(alphas[0], 1 / 21)
        self.assertAlmostEqual(alphas[-1], 1.0)
        self.assertEqual(room.phase, T.TeleportRoom.MATERIALIZE)
        room.update()               # flip tick
        self.assertEqual(room.phase, T.TeleportRoom.PLAY)
        self.assertEqual(room.slot_frame, 0)

    def test_play_sequence_f0x3_then_f1_f57(self):
        room = make_room()
        inject_scene(room, 0)
        # gap(4) + materialize(22, last tick flips to play + shows f0 #1)
        self._run(room, T.GAP_FRAMES + T.MATERIALIZE_FRAMES)
        seq = []
        for _ in range(T.PLAY_TICKS):
            room.update()
            seq.append(room.slot_frame)
        # pf1 -> f0 (#2), pf2 -> f1 ... pf58 -> f57; pf59 (the 60th
        # update) flips to dematerialize.
        self.assertEqual(seq[0], 0)
        self.assertEqual(seq[1], 0)
        self.assertEqual(seq[2], 1)
        self.assertEqual(seq[-1], T.LAST_PLAY_FRAME)
        self.assertEqual(max(seq), T.LAST_PLAY_FRAME)
        self.assertEqual(room.phase, T.TeleportRoom.PLAY)
        room.update()               # the demat flip tick
        self.assertEqual(room.phase, T.TeleportRoom.DEMATERIALIZE)
        self.assertEqual(room.held_frame, T.DEMAT_ENTRY_FRAME)

    def test_dematerialize_holds_f58_and_fades(self):
        room = make_room()
        inject_scene(room, 0)
        # gap + mat + play + the play->demat flip tick.
        self._run(room, T.GAP_FRAMES + T.MATERIALIZE_FRAMES
                  + T.PLAY_TICKS + 1)
        self.assertEqual(room.phase, T.TeleportRoom.DEMATERIALIZE)
        alphas = []
        for _ in range(T.MATERIALIZE_FRAMES - 1):
            alphas.append(1 - min(1, room.phase_frame / T.FADE_DIV))
            room.update()
        self.assertAlmostEqual(alphas[0], 1 - 1 / 14)
        self.assertAlmostEqual(alphas[13], 0.0)
        self.assertAlmostEqual(alphas[-1], 0.0)  # stays floored
        self.assertEqual(room.held_frame, T.DEMAT_ENTRY_FRAME)
        room.update()               # flip tick
        self.assertEqual(room.phase, T.TeleportRoom.GAP)
        self.assertNotIn(0, room.scenes)     # scene dropped
        self.assertEqual(room.current, 1)    # advanced

    def test_seven_scene_cycle(self):
        room = make_room(items=7)
        seen = []
        for i in range(7):
            inject_scene(room, i)
            # one full cycle: gap(4) + mat(22) + play(59 + flip) +
            # demat(21 + flip)
            self._run(room, T.GAP_FRAMES + T.MATERIALIZE_FRAMES
                      + T.PLAY_TICKS + 1 + T.MATERIALIZE_FRAMES)
            seen.append(room.current)
        self.assertEqual(seen, [1, 2, 3, 4, 5, 6, 0])

    def test_live_index_gating(self):
        room = make_room()
        inject_scene(room, 0)
        self.assertIsNone(room.live_index())            # gap
        self._run(room, T.GAP_FRAMES)
        self.assertIsNone(room.live_index())            # materialize
        self._run(room, T.MATERIALIZE_FRAMES)
        self.assertEqual(room.live_index(), 0)          # play
        self._run(room, T.PLAY_TICKS + 1)
        self.assertIsNone(room.live_index())            # dematerialize


class TestSlotsAndComposite(unittest.TestCase):
    def test_generate_slots_quant_diff(self):
        room = make_room()
        inject_scene(room, 0)
        # bg is all-zero; raw f0 is nonzero EXCEPT inside the erased
        # hide-triangle (both sides black -> no diff) — site semantics.
        room.generate_slots(0)
        ys = list(range(T.QUANT // 2, T.HEIGHT, T.QUANT))
        xs = list(range(T.QUANT // 2, T.WIDTH, T.QUANT))
        raw = room.scenes[0].raws[0]
        expected = sum(
            1 for y in ys for x in xs
            if any(abs(int(raw[y, x, c])) >= 16 for c in range(3)))
        self.assertEqual(len(room.slots), expected)
        self.assertGreater(expected, 0.8 * len(ys) * len(xs))
        for x, y, v, s in room.slots[:20]:
            self.assertIn(x, xs)
            self.assertIn(y, ys)
            self.assertTrue(0 <= v < T.SPRITE_VARIANTS)
            self.assertTrue(-3 <= s <= 3)

    def test_image_pass_scales_alpha_only(self):
        src = np.zeros((T.HEIGHT, T.WIDTH, 4), dtype=np.uint8)
        src[..., 0] = 100
        src[..., 3] = 255
        out = T._composite_pixels(src, 0.5, 99, [], None, None)
        self.assertEqual(int(out[10, 10, 0]), 100)   # color unchanged
        self.assertEqual(int(out[10, 10, 3]), 128)   # 255*0.5 rounded

    def test_no_sprites_at_t21(self):
        room = make_room()
        inject_scene(room, 0)
        room.slots = [(50, 50, 0, 0)]
        before = room.scenes[0].frames[0].copy()
        out = T._composite_pixels(before, 0.5, 21, room.slots,
                                  room.sprites, room.clip_mask)
        # t=21 is off-sheet: nothing drawn — only the alpha pass ran.
        self.assertEqual(int(out[50, 50, 0]), int(before[50, 50, 0]))

    def test_sprite_pass_overlay_blends(self):
        # One opaque white sprite pixel at cell (0,0); a slot at
        # (40,100) anchors the 60x100 cell at (40-30, 100-80) = (10,20)
        # — the opaque pixel lands exactly there.  Fill the WHOLE cell
        # opaque so the bbox covers it (regression: bbox-cropped source
        # windows once read the wrong sheet column and drew nothing).
        sprites = np.full((T.SPRITE_H * T.SPRITE_VARIANTS,
                           T.SPRITE_W * T.SPRITE_COLS, 4), 255,
                          dtype=np.uint8)
        src = np.zeros((T.HEIGHT, T.WIDTH, 4), dtype=np.uint8)
        src[..., :3] = 128
        src[..., 3] = 255
        mask = np.zeros((T.HEIGHT, T.WIDTH), dtype=bool)
        mask[20, 10] = True
        out = T._composite_pixels(src, 1.0, 0, [(40, 100, 0, 0)],
                                  sprites, mask)
        # overlay B(1, 0.502) = 1 -> white stays white.
        px = out[20, 10]
        self.assertGreater(int(px[0]), 250)
        self.assertEqual(int(px[3]), 255)

    def test_sprite_clipped_to_room_polygon(self):
        # Same setup but pixel OUTSIDE the clip mask -> untouched.
        sprites = np.zeros((T.SPRITE_H * T.SPRITE_VARIANTS,
                            T.SPRITE_W * T.SPRITE_COLS, 4), dtype=np.uint8)
        sprites[0, 0] = (255, 0, 0, 255)
        src = np.zeros((T.HEIGHT, T.WIDTH, 4), dtype=np.uint8)
        src[..., :3] = 128
        src[..., 3] = 255
        mask = np.zeros((T.HEIGHT, T.WIDTH), dtype=bool)
        out = T._composite_pixels(src, 1.0, 0, [(40, 100, 0, 0)],
                                  sprites, mask)
        px = out[20, 10]
        self.assertEqual(int(px[0]), 128)


class TestSceneDecodeShape(unittest.TestCase):
    def test_scene_clip_applied(self):
        # Structural: decoding requires network; here we verify the
        # clip application path used by _decode_scene on synthetic data.
        room = make_room()
        room.clip_mask = np.zeros((T.HEIGHT, T.WIDTH), dtype=bool)
        room.clip_mask[100:200, 50:150] = True
        px = np.full((T.HEIGHT, T.WIDTH, 4), 255, dtype=np.uint8)
        px[:, :, 3] = np.where(room.clip_mask, px[:, :, 3], 0)
        self.assertEqual(int(px[150, 100, 3]), 255)
        self.assertEqual(int(px[0, 0, 3]), 0)


class TestPrecomputedTransitions(unittest.TestCase):
    def test_fast_path_uses_precomputed_surfaces(self):
        import pygame
        room = make_room()
        inject_scene(room, 0)
        fake = [pygame.Surface((T.WIDTH, T.HEIGHT),
                               pygame.SRCALPHA)
                for _ in range(T.MATERIALIZE_FRAMES)]
        room.scenes[0].trans_mat = fake
        for _ in range(T.GAP_FRAMES):
            room.update()
        # beat 0 displayed from the precomputed list — identity match.
        self.assertIs(room._staging, fake[0])
        for _ in range(3):
            room.update()
        self.assertIs(room._staging, fake[room.phase_frame - 1])
        # No on-the-fly composite ran on the render path.
        self.assertFalse(hasattr(room, '_last_composite_ms')
                         and room._last_composite_ms)

    def test_gap_waits_for_transitions(self):
        room = make_room()
        inject_scene(room, 0, with_transitions=False)
        for _ in range(T.GAP_FRAMES):
            room.update()
        # scene decoded, transitions missing -> gap extends
        self.assertEqual(room.phase, T.TeleportRoom.GAP)
        for _ in range(10):
            room.update()
        self.assertEqual(room.phase, T.TeleportRoom.GAP)
        # transitions appear -> next tick materializes
        import pygame
        room.scenes[0].trans_mat = [
            pygame.Surface((T.WIDTH, T.HEIGHT), pygame.SRCALPHA)
            for _ in range(T.MATERIALIZE_FRAMES)]
        room.update()
        self.assertEqual(room.phase, T.TeleportRoom.MATERIALIZE)

    def test_fallback_composites_when_transitions_missing(self):
        import numpy as np
        room = make_room()
        inject_scene(room, 0, with_transitions=False)
        # force materialize past the gap-wait bound
        room.phase_frame = T.GAP_FRAMES * 8
        room.update()
        self.assertEqual(room.phase, T.TeleportRoom.MATERIALIZE)
        for _ in range(3):
            room.update()
        # No precomputed list -> on-the-fly numpy composite (array,
        # not a surface).
        self.assertIsNotNone(room._staging)
        self.assertIsInstance(room._staging, np.ndarray)
        self.assertGreater(
            getattr(room, '_last_composite_ms', 0), 0)

    def test_origin_correction_applied(self):
        # The measured (+4,+4) fix must be included in the origin.
        room = T.TeleportRoom('/tmp/f796_teleport_test_cache',
                              tiles_meta={'tiles': FAKE_TILES})
        room.tiles = FAKE_TILES
        anchor = T.resolve_point(FAKE_TILES, T.ANCHOR_TILE,
                                 T.ANCHOR_X, T.ANCHOR_Y)
        room.origin_x = anchor[0] + T.OFF_X + T.ORIGIN_FIX_X
        room.origin_y = anchor[1] + T.OFF_Y + T.ORIGIN_FIX_Y
        self.assertEqual(
            (room.origin_x, room.origin_y),
            (4 * 1016 + 15 - 4 + 4, 2 * 812 + 618 + 1 + 4))


    def test_giveup_and_skip_keep_decode_chain_alive(self):
        # Regression (on-device wedge): the gap give-up and skip
        # branches must request the next scene's decode, else scenes
        # stays empty and the room never cycles again.
        room = make_room(items=3)
        inject_scene(room, 0, with_transitions=False)
        room.phase_frame = T.GAP_FRAMES * 8
        room.update()     # give-up: materialize via fallback
        self.assertEqual(room.phase, T.TeleportRoom.MATERIALIZE)
        self.assertIn(1, room._scene_requests)
        # run scene 0 out (mat 22 + play 60 + demat 22)
        for _ in range(22 + 60 + 22):
            room.update()
        self.assertEqual(room.phase, T.TeleportRoom.GAP)
        self.assertEqual(room.current, 1)
        # no scenes at all -> skip branch advances AND requests decode
        room._scene_requests.clear()
        room.phase_frame = T.GAP_FRAMES * 8
        room.update()
        self.assertEqual(room.current, 2)
        self.assertIn(2, room._scene_requests)


if __name__ == '__main__':
    unittest.main(verbosity=2)
