// Entry point of the Cubie side panel in Google Meet (`meet-addon.html`).
//
// A SECOND Vite entry, not a route in the Cubie SPA: the panel runs in an
// iframe inside Meet, so it must not boot the app shell — no rail, no
// notifications polling, no /login redirect, no transcript-ingestion trigger on
// sign-in. It shares the stylesheet and the Google client id, nothing else.
// Served at /meet-addon/side-panel by `serve_frontend` in backend/main.py; in
// local dev open /meet-addon.html?meeting_code=abc-defg-hij.
import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { GoogleOAuthProvider } from '@react-oauth/google';
// Cubie's type pair, self-hosted: Montserrat for body text, Unbounded for the
// wordmark, titles and highlights (index.css names them).
import '@fontsource-variable/montserrat';
import '@fontsource-variable/montserrat/wght-italic.css';
import '@fontsource-variable/unbounded';
import '../index.css';
import { MeetAddonApp } from './MeetAddonApp';

const GOOGLE_CLIENT_ID = import.meta.env.VITE_GOOGLE_CLIENT_ID || 'MOCK_CLIENT_ID';

createRoot(document.getElementById('root')!).render(
    <StrictMode>
        <GoogleOAuthProvider clientId={GOOGLE_CLIENT_ID}>
            <MeetAddonApp />
        </GoogleOAuthProvider>
    </StrictMode>,
);
