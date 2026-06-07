import { create } from 'zustand';

export interface Product {
    product_id: string;
    name: string;
    price: number;
    currency: string;
    image_url: string;
    product_url: string;
    in_stock: boolean;
}

export interface CartItem extends Product {
    qty: number;
}

interface AppState {
    cart: CartItem[];
    isCartOpen: boolean;
    addToCart: (product: Product) => void;
    removeFromCart: (productId: string) => void;
    toggleCart: () => void;
    clearCart: () => void;
}

export const useStore = create<AppState>((set) => ({
    cart: [],
    isCartOpen: false,
    addToCart: (product) => set((state) => {
        const existing = state.cart.find(item => item.product_id === product.product_id);
        if (existing) {
            return {
                cart: state.cart.map(item =>
                    item.product_id === product.product_id
                        ? { ...item, qty: item.qty + 1 }
                        : item
                )
            };
        }
        return { cart: [...state.cart, { ...product, qty: 1 }] };
    }),
    removeFromCart: (productId) => set((state) => ({
        cart: state.cart.filter(item => item.product_id !== productId)
    })),
    toggleCart: () => set((state) => ({ isCartOpen: !state.isCartOpen })),
    clearCart: () => set({ cart: [] })
}));