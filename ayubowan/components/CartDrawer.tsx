"use client";

import { X, Trash2 } from 'lucide-react';
import { useStore } from '@/store/useStore';

export default function CartDrawer() {
    const { cart, isCartOpen, toggleCart, removeFromCart } = useStore();

    const total = cart.reduce((sum, item) => sum + (item.price * item.qty), 0);

    if (!isCartOpen) return null;

    return (
        <div className="fixed inset-0 z-50 flex justify-end bg-black/50 transition-opacity">
            <div className="w-full max-w-md bg-white h-full shadow-2xl flex flex-col animate-in slide-in-from-right duration-300">

                <div className="flex items-center justify-between border-b p-4">
                    <h2 className="text-lg font-bold text-gray-800">Your Cart</h2>
                    <button onClick={toggleCart} className="p-2 hover:bg-gray-100 rounded-full">
                        <X className="h-5 w-5 text-gray-600" />
                    </button>
                </div>

                <div className="flex-1 overflow-y-auto p-4 space-y-4">
                    {cart.length === 0 ? (
                        <div className="text-center text-gray-500 mt-10">Your cart is empty</div>
                    ) : (
                        cart.map((item) => (
                            <div key={item.product_id} className="flex gap-4 border-b pb-4">
                                <img
                                    src={item.image_url}
                                    alt={item.name}
                                    className="h-20 w-20 object-cover rounded-md border"
                                />
                                <div className="flex-1">
                                    <h3 className="font-semibold text-gray-800 line-clamp-2">{item.name}</h3>
                                    <p className="text-orange-600 font-medium mt-1">
                                        {item.currency} {item.price.toLocaleString()}
                                    </p>
                                    <p className="text-sm text-gray-500">Qty: {item.qty}</p>
                                </div>
                                <button
                                    onClick={() => removeFromCart(item.product_id)}
                                    className="text-red-500 hover:text-red-700 self-start p-2"
                                >
                                    <Trash2 className="h-5 w-5" />
                                </button>
                            </div>
                        ))
                    )}
                </div>

                {cart.length > 0 && (
                    <div className="border-t p-4 bg-gray-50">
                        <div className="flex justify-between font-bold text-lg mb-4 text-gray-800">
                            <span>Total:</span>
                            <span>LKR {total.toLocaleString()}</span>
                        </div>
                        <button className="w-full bg-orange-600 text-white font-bold py-3 rounded-lg hover:bg-orange-700 transition">
                            Proceed to Checkout
                        </button>
                    </div>
                )}
            </div>
        </div>
    );
}