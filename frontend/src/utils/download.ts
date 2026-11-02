// Bare Node loads this module (download.test.ts): the product name comes from
// the import-free `brand/names.ts`.
import { PRODUCT_NAME } from '../brand/names.ts';

/** The filename a `Content-Disposition` header names (quoted or bare), or
 *  `fallback` when it names none or there is no header. */
export function filenameFromDisposition(disposition: unknown, fallback: string): string {
    const match = /filename="?([^";]+)"?/.exec(String(disposition ?? ''));
    return match?.[1] ?? fallback;
}

/** What an EOS upload template is saved as: the server's name (the Scorecard's
 *  names the team), else `<product>_<kind>_template.xlsx`, the server's own
 *  default. */
export function templateFilename(disposition: unknown, kind: 'Scorecard' | 'VTO'): string {
    return filenameFromDisposition(disposition, `${PRODUCT_NAME}_${kind}_template.xlsx`);
}

/** Trigger a browser download for a blob response.
 *
 *  The object URL is revoked on the next tick rather than immediately: some
 *  browsers start the download asynchronously after `click()`, and revoking
 *  first cancels it. */
export function downloadBlob(data: Blob, filename: string): void {
    const url = URL.createObjectURL(data);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 1_000);
}
