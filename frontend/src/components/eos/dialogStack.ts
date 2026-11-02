// The stack of open modal dialogs, shared by every dialog that traps focus:
// Modal (and ConfirmDialog, built on it) and the What's New drawer's full mode.
//
// A single Escape must close only the TOPMOST dialog — a confirm opened over a
// modal, or a modal opened from the drawer, must not close both — and only the
// topmost one may pull Tab focus into itself. Each dialog pushes an id when it
// opens, pops it when it closes, and returns early from its key handler unless
// `isTopmost(id)`. Non-modal surfaces (the drawer's weekly peek, popovers)
// never register.
//
// Import-free so it runs under Node's test runner (`dialogStack.test.ts`).

export type DialogId = number | string;

let seq = 0;
const stack: DialogId[] = [];

/** A fresh id for a dialog instance. */
export const newDialogId = (): number => ++seq;

export function push(id: DialogId): void {
    stack.push(id);
}

/** Remove the dialog (its latest entry), wherever it sits in the stack: a
 *  dialog underneath may close first. */
export function pop(id: DialogId): void {
    const idx = stack.lastIndexOf(id);
    if (idx !== -1) stack.splice(idx, 1);
}

export const isTopmost = (id: DialogId): boolean => stack.length > 0 && stack[stack.length - 1] === id;

/** How many dialogs are open. */
export const openDialogCount = (): number => stack.length;
