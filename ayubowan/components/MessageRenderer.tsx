import { ExternalLink, Package } from 'lucide-react';
import ProductCard from './ProductCard';
import { Product } from '@/store/useStore';

export default function MessageRenderer({ content }: { content: string }) {
    const parts = content.split(/(<PRODUCTS>[\s\S]*?<\/PRODUCTS>|<PAY_LINK>[\s\S]*?<\/PAY_LINK>|<TRACKING>[\s\S]*?<\/TRACKING>)/);

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

                if (part.startsWith('<PAY_LINK>')) {
                    const link = part.replace('<PAY_LINK>', '').replace('</PAY_LINK>', '').trim();
                    return (
                        <a
                            key={index}
                            href={link}
                            target="_blank"
                            rel="noreferrer"
                            className="flex items-center gap-2 w-fit bg-green-600 text-white px-4 py-3 rounded-lg font-bold hover:bg-green-700 transition shadow-md"
                        >
                            <ExternalLink className="h-5 w-5" />
                            Pay Securely on Kapruka
                        </a>
                    );
                }

                if (part.startsWith('<TRACKING>')) {
                    try {
                        const jsonString = part.replace('<TRACKING>', '').replace('</TRACKING>', '').trim();
                        const tracking = JSON.parse(jsonString);

                        return (
                            <div key={index} className="bg-white border rounded-lg p-4 shadow-sm">
                                <div className="flex items-center gap-2 border-b pb-2 mb-3">
                                    <Package className="h-5 w-5 text-orange-600" />
                                    <h3 className="font-bold text-gray-800">Order {tracking.order_number}</h3>
                                </div>
                                <p className="font-medium text-gray-700 mb-3">Status: <span className="text-orange-600">{tracking.status}</span></p>
                                <ul className="space-y-2 text-sm text-gray-600">
                                    {tracking.timeline.map((event: string, i: number) => (
                                        <li key={i} className="flex items-center gap-2">
                                            <div className="h-2 w-2 rounded-full bg-gray-300"></div>
                                            {event}
                                        </li>
                                    ))}
                                </ul>
                            </div>
                        );
                    } catch (e) {
                        return <div key={index} className="text-red-500 text-sm">Failed to load tracking data.</div>;
                    }
                }

                if (!part.trim()) return null;

                return (
                    <div key={index} className="whitespace-pre-wrap text-gray-800">
                        {part}
                    </div>
                );
            })}
        </div>
    );
}