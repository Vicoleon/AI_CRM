// The product's brand for the UI: one module, mirrored from the backend's
// `rubik_core/brand.py` (backend/tests/test_brand.py pins that they agree).
// The names live in `./names.ts`, which has no imports so bare Node can load
// it in tests.
import cubeMarkUrl from './assets/cube.svg';

export { PRODUCT_NAME, SUPPORT_EMAIL, VENDOR_NAME, VENDOR_URL } from './names.ts';

// The RubikSoft cube icon. Its path is filled with `currentColor` and the file
// defaults to brand violet, so as an <img> it shows violet; used as a CSS mask
// it takes whatever colour the element paints.
export const cubeMark: string = cubeMarkUrl;
