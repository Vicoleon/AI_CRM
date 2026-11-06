// The app's one Chart.js setup: element registration and the shared base
// options. Moved here from `pages/eos/ExecDashboard.tsx` so the EOS Exec
// Dashboard and the Insights dashboards draw with the same chrome — quiet grey
// axes, no gridlines across, a dark index tooltip — and so Chart.js is
// registered exactly once however many pages import it.
//
// `CHART_OPTIONS_BASE` is EXACTLY the object ExecDashboard defined: its charts
// spread `.plugins.tooltip`, `.scales.x` and `.scales.y.ticks`, so the shape is
// part of the contract. Change it and the Exec Dashboard changes with it.
import {
    Chart as ChartJS,
    CategoryScale,
    LinearScale,
    PointElement,
    LineElement,
    BarElement,
    Title,
    Tooltip,
    Legend,
    Filler,
} from 'chart.js';

// Chart.js needs explicit element/scale registration — opt-in keeps the
// bundle smaller than auto-importing everything.
ChartJS.register(
    CategoryScale,
    LinearScale,
    PointElement,
    LineElement,
    BarElement,
    Title,
    Tooltip,
    Legend,
    Filler,
);

export const CHART_OPTIONS_BASE = {
    responsive: true,
    maintainAspectRatio: false,
    plugins: {
        legend: { display: false },
        tooltip: {
            mode: 'index' as const,
            intersect: false,
            backgroundColor: 'rgba(17, 24, 39, 0.95)',
            titleFont: { size: 11 },
            bodyFont: { size: 11 },
        },
    },
    scales: {
        x: {
            grid: { display: false },
            ticks: { color: '#9ca3af', font: { size: 10 } },
        },
        y: {
            grid: { color: '#f3f4f6' },
            ticks: { color: '#9ca3af', font: { size: 10 } },
            beginAtZero: true,
        },
    },
};

/** Respect the OS "reduce motion" setting: Chart.js animates by default. */
export const prefersReducedMotion = (): boolean => {
    try {
        return typeof window !== 'undefined' && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    } catch {
        return false;
    }
};
