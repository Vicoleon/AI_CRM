# Cubie frontend

The React, Vite and Tailwind app for Cubie. In production it is built into
`dist/` and served by the FastAPI backend; locally it runs on its own.

```bash
npm ci
npm run dev      # http://localhost:5173, proxies /api to http://127.0.0.1:8001 (VITE_API_TARGET overrides)
npm test         # node --test over src/**/*.test.ts (Node 22 or newer)
npm run build    # type-checks the app and the tests, then builds dist/
npm run lint
```

- **Brand.** The product name, vendor and logo come from `src/brand/`; colours
  and fonts are CSS variables mapped onto Tailwind's `brand`, `rail` and `ink`
  colours and its `sans` (body) and `display` fonts. Never hard-code the product name or a brand
  colour in a page.
- **Feature gates.** Pages and nav rows read `features.<key>` from
  `/api/users/me` through `FEATURE` in `src/context/AuthContext.tsx`, never a
  department string.
- **Help and tours.** A user-visible change updates its Help topic in
  `src/pages/help/topics/` (mirrored in `rubik_core/help/content.py`) and ships
  a page tour in `src/tours/content/`.
- **Build-time settings:** `VITE_GOOGLE_CLIENT_ID` (Google sign-in) and
  `VITE_MEET_ADDON_CLOUD_PROJECT_NUMBER` (the Meet add-on). Neither has a
  default.

See the repository [README](../README.md) for local sign-in and the backend.
