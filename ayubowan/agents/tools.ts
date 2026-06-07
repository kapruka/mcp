// agents/tools.ts
import type { ChatCompletionTool } from 'openai/resources/chat/completions';

export const KAPRUKA_TOOLS: ChatCompletionTool[] = [
    {
        type: "function",
        function: {
            name: 'kapruka_search_products',
            description: 'Search Kapruka catalog by keyword. Returns product list with names, prices, images. Use for any product browsing request.',
            parameters: {
                type: 'object',
                properties: {
                    q: { type: 'string', description: 'Search keywords' },
                    category: { type: 'string', description: 'Category filter (optional)' },
                    min_price: { type: 'number', description: 'Minimum price in LKR' },
                    max_price: { type: 'number', description: 'Maximum price in LKR' },
                    in_stock_only: { type: 'boolean', description: 'Only show in-stock items' },
                    limit: { type: 'number', description: 'Results per page, max 20' }
                },
                required: ['q']
            }
        }
    },
    {
        type: "function",
        function: {
            name: 'kapruka_get_product',
            description: 'Get full details for a product by ID. Use when user wants to know more about a specific item.',
            parameters: {
                type: 'object',
                properties: {
                    product_id: { type: 'string' }
                },
                required: ['product_id']
            }
        }
    },
    {
        type: "function",
        function: {
            name: 'kapruka_list_categories',
            description: 'List all Kapruka shopping categories. Use when user wants to browse or is unsure what to buy.',
            parameters: {
                type: 'object',
                properties: {
                    depth: { type: 'number' }
                }
            }
        }
    },
    {
        type: "function",
        function: {
            name: 'kapruka_list_delivery_cities',
            description: 'Find the canonical city name for delivery. Always run this before check_delivery.',
            parameters: {
                type: 'object',
                properties: {
                    query: { type: 'string', description: 'City name the user typed' }
                },
                required: ['query']
            }
        }
    },
    {
        type: "function",
        function: {
            name: 'kapruka_check_delivery',
            description: 'Check if delivery is available to a city on a specific date and get the fee.',
            parameters: {
                type: 'object',
                properties: {
                    city: { type: 'string', description: 'Canonical city name from kapruka_list_delivery_cities' },
                    delivery_date: { type: 'string', description: 'Date in YYYY-MM-DD format' },
                    product_id: { type: 'string', description: 'Optional: product ID for perishable check' }
                },
                required: ['city', 'delivery_date']
            }
        }
    },
    {
        type: "function",
        function: {
            name: 'kapruka_create_order',
            description: 'Create a guest checkout order. Returns a click-to-pay URL valid for 60 minutes.',
            parameters: {
                type: 'object',
                properties: {
                    cart: {
                        type: 'array',
                        items: {
                            type: 'object',
                            properties: {
                                product_id: { type: 'string' },
                                qty: { type: 'number' }
                            },
                            required: ['product_id', 'qty']
                        }
                    },
                    recipient: {
                        type: 'object',
                        properties: {
                            name: { type: 'string' },
                            phone: { type: 'string' },
                            address: { type: 'string' },
                            city: { type: 'string' }
                        },
                        required: ['name', 'phone', 'address', 'city']
                    },
                    delivery: {
                        type: 'object',
                        properties: {
                            date: { type: 'string' }
                        },
                        required: ['date']
                    },
                    gift_message: { type: 'string' }
                },
                required: ['cart', 'recipient', 'delivery']
            }
        }
    },
    {
        type: "function",
        function: {
            name: 'kapruka_track_order',
            description: 'Track an existing order by order number. Returns status and timeline.',
            parameters: {
                type: 'object',
                properties: {
                    order_number: { type: 'string' }
                },
                required: ['order_number']
            }
        }
    }
];