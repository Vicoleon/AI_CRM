import React, { useState, useEffect } from 'react';
import { Plus, Building2, Trash2 } from 'lucide-react';
import client from '../api/client';
import { motion, AnimatePresence } from 'framer-motion';

export const Departments: React.FC = () => {
    const [departments, setDepartments] = useState<string[]>([]);
    const [newDepartment, setNewDepartment] = useState('');
    const [loading, setLoading] = useState(false);

    useEffect(() => {
        fetchDepartments();
    }, []);

    const fetchDepartments = async () => {
        try {
            const { data } = await client.get('/api/departments/');
            setDepartments(data);
        } catch (err) {
            console.error('Failed to fetch departments:', err);
        }
    };

    const handleAdd = async (e: React.FormEvent) => {
        e.preventDefault();
        if (!newDepartment.trim()) return;

        setLoading(true);
        try {
            await client.post('/api/departments/', { department: newDepartment });
            setNewDepartment('');
            fetchDepartments();
        } catch (err) {
            console.error('Failed to add department:', err);
        } finally {
            setLoading(false);
        }
    };

    const handleRemove = async (dept: string) => {
        try {
            await client.delete(`/api/departments/${encodeURIComponent(dept)}`);
            fetchDepartments();
        } catch (err) {
            console.error('Failed to remove department:', err);
        }
    };

    /* Rendered inside the Administration content card (Admin → Departments), not
       as a standalone page any more — so no outer padding and no page-level h1.
       The tab label already names the section; a 32px heading inside the card
       would out-shout the "Administration" title above it. Matches the bare
       description paragraph the other admin tabs open with. */
    return (
        <div>
            <p className="text-[#64748b] text-[15px] mb-6">Manage organization departments</p>

            {/* Add Department Form */}
            <div className="bg-white rounded-[12px] border border-gray-100 p-6 mb-8 w-fit shadow-[0_2px_8px_rgb(0,0,0,0.02)]">
                <form onSubmit={handleAdd} className="flex gap-4 items-center">
                    <input
                        type="text"
                        value={newDepartment}
                        onChange={(e) => setNewDepartment(e.target.value)}
                        placeholder="Enter new department name..."
                        className="w-[320px] border border-gray-200 rounded-[6px] px-4 py-2.5 text-[14px] text-gray-700 placeholder-gray-400 focus:outline-none focus:border-brand/50 focus:ring-2 focus:ring-brand/10 transition-all font-medium"
                    />
                    <button
                        type="submit"
                        disabled={loading || !newDepartment.trim()}
                        className="bg-brand hover:bg-brand-hover text-white text-[14px] font-medium px-5 py-2.5 rounded-[6px] transition-colors disabled:opacity-50 disabled:cursor-not-allowed flex items-center gap-2"
                    >
                        <Plus strokeWidth={2.5} size={16} />
                        Add Department
                    </button>
                </form>
            </div>

            {/* Departments Table */}
            <div className="overflow-x-auto bg-white border border-gray-100 rounded-lg">
                <table className="w-full text-left">
                    <thead>
                        <tr className="text-gray-500 text-sm border-b border-gray-100 bg-gray-50/50">
                            <th className="py-3 px-4 font-semibold w-3/4">Department Name</th>
                            <th className="py-3 px-4 font-semibold text-center w-1/4">Action</th>
                        </tr>
                    </thead>
                    <tbody className="divide-y divide-gray-50">
                        <AnimatePresence>
                            {departments.map((dept) => (
                                <motion.tr
                                    key={dept}
                                    initial={{ opacity: 0, y: -10 }}
                                    animate={{ opacity: 1, y: 0 }}
                                    exit={{ opacity: 0, y: 10 }}
                                    className="group hover:bg-gray-50/50 transition-colors"
                                >
                                    <td className="py-3 px-4">
                                        <div className="flex items-center gap-3">
                                            <div className="h-9 w-9 bg-brand-tint rounded-full flex items-center justify-center text-brand text-sm font-bold shadow-sm">
                                                <Building2 size={16} strokeWidth={2.5} />
                                            </div>
                                            <div className="flex flex-col">
                                                <span className="font-semibold text-sm text-gray-900">{dept}</span>
                                            </div>
                                        </div>
                                    </td>
                                    <td className="py-3 px-4 text-center">
                                        <button
                                            onClick={() => handleRemove(dept)}
                                            className="text-gray-400 hover:text-red-600 transition-colors p-1.5 rounded hover:bg-red-50"
                                            title="Delete Department"
                                        >
                                            <Trash2 className="h-4 w-4" />
                                        </button>
                                    </td>
                                </motion.tr>
                            ))}
                        </AnimatePresence>
                    </tbody>
                </table>
            </div>

            {departments.length === 0 && (
                <div className="text-center py-12 text-gray-500 bg-white rounded-xl border border-gray-100 shadow-sm mt-8">
                    <Building2 size={40} className="mx-auto mb-3 text-gray-300" strokeWidth={1.5} />
                    <p className="text-[15px]">No departments yet. Add your first department above.</p>
                </div>
            )}
        </div>
    );
};
