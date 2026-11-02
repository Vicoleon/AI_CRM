// Regression tests for the voice command shortcut.
//
//   cd <repo>/frontend && node --test "src/components/voice/shortcut.test.ts"
//
// What is pinned here:
//
//   * The default is Option+Command on a Mac and Ctrl+Alt on a PC, and a saved
//     choice (or "off") replaces it for that kind of keyboard only.
//   * Modifiers alone count only when pressed and let go with no other key in
//     between, so ⌥⌘I (dev tools) or AltGr+Q (typing "@" on a German keyboard,
//     which Windows reports as Ctrl+Alt+Q) never starts the microphone.
//   * A chord with a key fires on that key, never on its auto-repeat.
//   * Recording a new shortcut in Settings refuses one that would fire while
//     typing (a bare key, a single modifier).
import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import {
    ChordRecorder,
    ChordTracker,
    DEFAULT_CHORDS,
    chordLabel,
    chordWords,
    effectiveChord,
    keyLabel,
} from './shortcut.ts';
import type { Chord, KeyLike, Modifier } from './shortcut.ts';

const CODE_OF: Record<Modifier, string> = {
    alt: 'AltLeft', ctrl: 'ControlLeft', meta: 'MetaLeft', shift: 'ShiftLeft',
};

/** A key event with `held` modifiers down (after the event, as browsers report). */
const ev = (code: string, held: Modifier[], extra: Partial<KeyLike> = {}): KeyLike => ({
    code,
    altKey: held.includes('alt'),
    ctrlKey: held.includes('ctrl'),
    metaKey: held.includes('meta'),
    shiftKey: held.includes('shift'),
    ...extra,
});

/** Press modifiers one by one, then let them go in the same order. */
function tap(tracker: ChordTracker, mods: Modifier[], between: (held: Modifier[]) => string[] = () => []) {
    const signals: (string | null)[] = [];
    const held: Modifier[] = [];
    for (const m of mods) {
        held.push(m);
        signals.push(tracker.handle('keydown', ev(CODE_OF[m], held)));
    }
    for (const code of between(held)) signals.push(tracker.handle('keydown', ev(code, held)));
    for (const m of mods) {
        held.splice(held.indexOf(m), 1);
        signals.push(tracker.handle('keyup', ev(CODE_OF[m], held)));
    }
    return signals.filter(Boolean);
}

describe('effectiveChord', () => {
    it('defaults to Option+Command on a Mac and Ctrl+Alt on a PC', () => {
        assert.deepEqual(effectiveChord(null, 'mac'), { mods: ['alt', 'meta'], code: null });
        assert.deepEqual(effectiveChord(undefined, 'pc'), { mods: ['ctrl', 'alt'], code: null });
    });

    it('uses a saved choice for its own keyboard only', () => {
        const mine: Chord = { mods: ['ctrl'], code: 'Space' };
        assert.deepEqual(effectiveChord({ mac: mine }, 'mac'), mine);
        assert.deepEqual(effectiveChord({ mac: mine }, 'pc'), DEFAULT_CHORDS.pc);
    });

    it('is off when turned off', () => {
        assert.equal(effectiveChord({ pc: 'off' }, 'pc'), null);
    });
});

describe('labels', () => {
    it('reads the Mac way and the PC way', () => {
        assert.equal(chordLabel(DEFAULT_CHORDS.mac, 'mac'), '⌥⌘');
        assert.equal(chordLabel(DEFAULT_CHORDS.pc, 'pc'), 'Ctrl+Alt');
        assert.equal(chordLabel({ mods: ['meta', 'shift'], code: 'KeyK' }, 'mac'), '⇧⌘K');
        assert.equal(chordWords(DEFAULT_CHORDS.mac, 'mac'), 'Option+Command');
        assert.equal(keyLabel('Digit7'), '7');
        assert.equal(keyLabel('Space'), 'Space');
    });
});

describe('ChordTracker: modifiers alone', () => {
    it('arms when the chord is down and releases when it is let go', () => {
        const t = new ChordTracker(DEFAULT_CHORDS.mac);
        assert.deepEqual(tap(t, ['alt', 'meta']), ['arm', 'release']);
        // in either order, and again
        assert.deepEqual(tap(t, ['meta', 'alt']), ['arm', 'release']);
    });

    it('cancels when another key joins it (⌥⌘I opens dev tools)', () => {
        const t = new ChordTracker(DEFAULT_CHORDS.mac);
        assert.deepEqual(tap(t, ['alt', 'meta'], () => ['KeyI']), ['arm', 'cancel']);
    });

    it('never fires for AltGr+Q, which Windows reports as Ctrl+Alt+Q', () => {
        const t = new ChordTracker(DEFAULT_CHORDS.pc);
        assert.deepEqual(tap(t, ['ctrl', 'alt'], () => ['KeyQ']), ['arm', 'cancel']);
    });

    it('does not re-arm in the same hold after another key was pressed', () => {
        const t = new ChordTracker(DEFAULT_CHORDS.mac);
        // Hold Command, type C (copy), then add Option: not a clean chord.
        assert.equal(t.handle('keydown', ev('MetaLeft', ['meta'])), null);
        assert.equal(t.handle('keydown', ev('KeyC', ['meta'])), null);
        assert.equal(t.handle('keydown', ev('AltLeft', ['meta', 'alt'])), null);
        t.handle('keyup', ev('AltLeft', ['meta']));
        t.handle('keyup', ev('MetaLeft', []));
        // All up: the next clean press works.
        assert.deepEqual(tap(t, ['alt', 'meta']), ['arm', 'release']);
    });

    it('cancels when a further modifier joins (⌥⇧⌘ is something else)', () => {
        const t = new ChordTracker(DEFAULT_CHORDS.mac);
        assert.deepEqual(tap(t, ['alt', 'meta', 'shift']), ['arm', 'cancel']);
    });

    it('ignores key auto-repeat and IME composition', () => {
        const t = new ChordTracker(DEFAULT_CHORDS.mac);
        t.handle('keydown', ev('AltLeft', ['alt']));
        assert.equal(t.handle('keydown', ev('MetaLeft', ['alt', 'meta'])), 'arm');
        assert.equal(t.handle('keydown', ev('MetaLeft', ['alt', 'meta'], { repeat: true })), null);
        assert.equal(t.handle('keydown', ev('KeyA', ['alt', 'meta'], { isComposing: true })), null);
        assert.equal(t.handle('keyup', ev('MetaLeft', ['alt'])), 'release');
    });
});

describe('ChordTracker: modifiers with a key', () => {
    const ctrlSpace: Chord = { mods: ['ctrl'], code: 'Space' };

    it('arms on the key, once, and releases when the key comes up', () => {
        const t = new ChordTracker(ctrlSpace);
        t.handle('keydown', ev('ControlLeft', ['ctrl']));
        assert.equal(t.handle('keydown', ev('Space', ['ctrl'])), 'arm');
        assert.equal(t.handle('keydown', ev('Space', ['ctrl'], { repeat: true })), null);
        assert.equal(t.handle('keyup', ev('Space', ['ctrl'])), 'release');
    });

    it('needs exactly its modifiers', () => {
        const t = new ChordTracker(ctrlSpace);
        assert.equal(t.handle('keydown', ev('Space', [])), null);
        assert.equal(t.handle('keydown', ev('Space', ['ctrl', 'shift'])), null);
    });
});

describe('ChordRecorder', () => {
    const press = (r: ChordRecorder, mods: Modifier[], key?: string, platform: 'mac' | 'pc' = 'mac') => {
        const held: Modifier[] = [];
        let out = null;
        for (const m of mods) {
            held.push(m);
            out = r.handle('keydown', ev(CODE_OF[m], held), platform) ?? out;
        }
        if (key) out = r.handle('keydown', ev(key, held), platform) ?? out;
        for (const m of [...mods]) {
            held.splice(held.indexOf(m), 1);
            out = r.handle('keyup', ev(CODE_OF[m], held), platform) ?? out;
        }
        return out;
    };
    const refused = (step: unknown) => !!step && typeof step === 'object' && 'error' in step;

    it('records two modifiers alone', () => {
        assert.deepEqual(press(new ChordRecorder(), ['meta', 'alt']), { chord: { mods: ['alt', 'meta'], code: null } });
    });

    it('records modifiers with a key', () => {
        assert.deepEqual(press(new ChordRecorder(), ['ctrl'], 'Space'), { chord: { mods: ['ctrl'], code: 'Space' } });
    });

    it('refuses a single modifier and a bare key', () => {
        assert.ok(refused(press(new ChordRecorder(), ['shift'])));
        assert.ok(refused(press(new ChordRecorder(), [], 'KeyK')));
    });

    it('refuses chords that type a character (Shift+A, Option+V, AltGr)', () => {
        assert.ok(refused(press(new ChordRecorder(), ['shift'], 'KeyA')));
        assert.ok(refused(press(new ChordRecorder(), ['alt'], 'KeyV')));
        assert.ok(refused(press(new ChordRecorder(), ['ctrl', 'alt'], 'KeyQ', 'pc')));
    });

    it("refuses the browser's own shortcuts but takes them with Shift", () => {
        assert.ok(refused(press(new ChordRecorder(), ['meta'], 'KeyC')));
        assert.ok(refused(press(new ChordRecorder(), ['ctrl'], 'KeyV', 'pc')));
        assert.deepEqual(press(new ChordRecorder(), ['meta', 'shift'], 'KeyK'), { chord: { mods: ['shift', 'meta'], code: 'KeyK' } });
        assert.deepEqual(press(new ChordRecorder(), ['ctrl'], 'Space', 'pc'), { chord: { mods: ['ctrl'], code: 'Space' } });
    });

    it('gives up on Escape', () => {
        assert.deepEqual(new ChordRecorder().handle('keydown', ev('Escape', []), 'mac'), { cancelled: true });
    });
});
