import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
// Cubie's type pair, self-hosted: Montserrat for body text, Unbounded for the
// wordmark, titles and highlights (index.css names them).
import '@fontsource-variable/montserrat'
import '@fontsource-variable/montserrat/wght-italic.css'
import '@fontsource-variable/unbounded'
import './index.css'
import App from './App.tsx'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
