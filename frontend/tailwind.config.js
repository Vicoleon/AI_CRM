/** @type {import('tailwindcss').Config} */
export default {
    content: [
        "./index.html",
        "./meet-addon.html",
        "./src/**/*.{js,ts,jsx,tsx}",
    ],
    theme: {
        extend: {
            // Cubie's brand tokens. Each reads an RGB-channel CSS variable from
            // src/index.css, so an opacity modifier works (`bg-brand/10`) and a
            // workspace accent can override the variables after sign-in.
            colors: {
                brand: {
                    DEFAULT: 'rgb(var(--brand) / <alpha-value>)',
                    hover: 'rgb(var(--brand-hover) / <alpha-value>)',
                    tint: 'rgb(var(--brand-tint) / <alpha-value>)',
                    soft: 'rgb(var(--brand-soft) / <alpha-value>)',
                },
                accent: 'rgb(var(--accent) / <alpha-value>)',
                rail: {
                    DEFAULT: 'rgb(var(--rail) / <alpha-value>)',
                    active: 'rgb(var(--rail-active) / <alpha-value>)',
                },
                ink: 'rgb(var(--ink) / <alpha-value>)',
            },
            fontFamily: {
                sans: 'var(--font-body)',
                display: 'var(--font-display)',
            }
        },
    },
    plugins: [require('@tailwindcss/typography')],
}
