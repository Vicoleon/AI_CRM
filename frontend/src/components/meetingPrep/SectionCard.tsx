import React from 'react';
import { motion } from 'framer-motion';
import { cn } from './cn';

export type SectionIcon = React.ComponentType<{ className?: string }>;

export interface SectionCardProps {
    icon: SectionIcon;
    title: string;
    children: React.ReactNode;
    className?: string;
    /** The dark card (external Product Fit): muted icon, light controls. */
    dark?: boolean;
    /** Right side of the header row — provenance badge, edit/rewrite buttons. */
    headerRight?: React.ReactNode;
}

/**
 * One card of the brief. The `SectionCard` class name is a hook for the
 * page's print CSS (`page-break-inside: avoid`), so keep it.
 */
export const SectionCard: React.FC<SectionCardProps> = ({
    icon: Icon,
    title,
    children,
    className,
    dark,
    headerRight,
}) => (
    <motion.div
        initial={{ opacity: 0, y: 20 }}
        animate={{ opacity: 1, y: 0 }}
        className={cn(
            'SectionCard bg-white rounded-2xl border border-slate-200 p-6 shadow-sm transition-all hover:shadow-md',
            className,
        )}
    >
        <div className="flex items-center gap-2 mb-4">
            <div className={cn('p-2 rounded-lg shrink-0', dark ? 'bg-white/10' : 'bg-brand/10')}>
                <Icon className={cn('w-5 h-5', dark ? 'text-slate-400' : 'text-brand')} />
            </div>
            <h3 className="text-lg font-bold text-inherit min-w-0">{title}</h3>
            {headerRight && <div className="ml-auto flex items-center gap-1.5 shrink-0">{headerRight}</div>}
        </div>
        {children}
    </motion.div>
);
