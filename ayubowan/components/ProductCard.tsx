"use client";

import { ShoppingBag } from 'lucide-react';
import { useStore, Product } from '@/store/useStore';

export default function ProductCard({ product }: { product: Product }) {
    const { addToCart } = useStore();

    return (
        <div className="flex flex-col rounded-lg border bg-white shadow-sm overflow-hidden w-64 flex-shrink-0">
            <img src={product.image_url} alt={product.name} className="h-48 w-full object-cover" />
            <div className="p-4 flex flex-col flex-1">
                <h3 className="font-semibold text-gray-800 line-clamp-2 text-sm">{product.name}</h3>
                <p className="text-orange-600 font-bold mt-2">
                    {product.currency} {product.price.toLocaleString()}
                </p>
                <div className="mt-auto pt-4 space-y-2">
                    <button
                        onClick={() => addToCart(product)}
                        className="w-full flex items-center justify-center gap-2 rounded-md bg-orange-600 px-3 py-2 text-sm font-medium text-white hover:bg-orange-700 transition"
                    >
                        <ShoppingBag className="h-4 w-4" />
                        Add to Cart
                    </button>
                    <a
                        href={product.product_url}
                        target="_blank"
                        rel="noreferrer"
                        className="w-full flex items-center justify-center rounded-md border border-gray-300 px-3 py-2 text-sm font-medium text-gray-700 hover:bg-gray-50 transition"
                    >
                        View Details
                    </a>
                </div>
            </div>
        </div>
    );
}