// The product's names and support contact, with no imports, so modules that
// bare Node loads in tests (`node --test`) can read them too. `brand.ts`
// re-exports them; import from there in components. Mirrors the backend's
// `rubik_core/brand.py` (backend/tests/test_brand.py pins that they agree).

export const PRODUCT_NAME = 'Cubie';
export const VENDOR_NAME = 'RubikSoft';
export const VENDOR_URL = 'https://www.rubik-soft.com';
// Where a workspace admin reaches RubikSoft. The static privacy page
// (public/chrome-privacy.html) writes it out by hand; test_brand.py pins it.
export const SUPPORT_EMAIL = 'jose.leon@rubik-soft.com';
