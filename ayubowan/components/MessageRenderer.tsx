import ProductCard from './ProductCard';
import { Product } from '@/store/useStore';

export default function MessageRenderer({ content }: { content: string }) {
    const parts = content.split(/(<PRODUCTS>[\s\S]*?<\/PRODUCTS>)/);

    return (
        <div className="space-y-4">
            {parts.map((part, index) => {
                if (part.startsWith('<PRODUCTS>')) {
                    try {
                        const jsonString = part.replace('<PRODUCTS>', '').replace('</PRODUCTS>', '').trim();
                        const products: Product[] = JSON.parse(jsonString);

                        return (
                            <div key={index} className="flex gap-4 overflow-x-auto pb-4 snap-x">
                                {products.map((p) => (
                                    <div key={p.product_id} className="snap-start">
                                        <ProductCard product={p} />
                                    </div>
                                ))}
                            </div>
                        );
                    } catch (e) {
                        return <div key={index} className="text-red-500 text-sm">Failed to load products.</div>;
                    }
                }

                return (
                    <div key={index} className="whitespace-pre-wrap text-gray-800">
                        {part}
                    </div>
                );
            })}
        </div>
    );
}