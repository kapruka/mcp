"use client";

import { ShoppingCart } from 'lucide-react';
import { useStore } from '@/store/useStore';

export default function Navbar() {
    const { cart, toggleCart } = useStore();
    const itemCount = cart.reduce((total, item) => total + item.qty, 0);

    return (
        <header className="flex items-center justify-between bg-white px-6 py-4 shadow-sm">
            <div className="text-2xl font-bold text-orange-600">Ayubowan</div>
            <button
                onClick={toggleCart}
                className="relative p-2 text-gray-600 hover:text-orange-600 transition-colors"
            >
                <ShoppingCart className="h-6 w-6" />
                {itemCount > 0 && (
                    <span className="absolute -right-1 -top-1 flex h-5 w-5 items-center justify-center rounded-full bg-orange-600 text-xs font-bold text-white shadow-md">
                        {itemCount}
                    </span>
                )}
            </button>
        </header>
    );
}