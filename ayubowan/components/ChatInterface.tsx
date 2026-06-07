"use client";

import { useState } from 'react';
import { Send } from 'lucide-react';

export default function ChatInterface() {
    const [input, setInput] = useState('');

    return (
        <div className="mx-auto flex h-full max-w-4xl flex-col bg-white shadow-lg sm:rounded-lg sm:border sm:border-gray-200 sm:mt-4 sm:h-[calc(100vh-6rem)]">
            <div className="flex-1 overflow-y-auto p-4 sm:p-6">
                <div className="mb-4 max-w-[80%] rounded-2xl rounded-tl-sm bg-orange-50 p-4 text-gray-800 shadow-sm border border-orange-100">
                    ආයුබෝවන්! 🌺 Welcome to Kapruka. What are you looking to buy today?
                </div>
            </div>

            <div className="border-t bg-gray-50 p-4 sm:rounded-b-lg">
                <div className="flex items-center gap-2 rounded-full border border-gray-300 bg-white px-4 py-2 shadow-sm focus-within:border-orange-500 focus-within:ring-1 focus-within:ring-orange-500 transition-all">
                    <input
                        type="text"
                        value={input}
                        onChange={(e) => setInput(e.target.value)}
                        placeholder="Type your message..."
                        className="flex-1 bg-transparent outline-none text-gray-700"
                    />
                    <button className="flex h-10 w-10 items-center justify-center rounded-full bg-orange-600 text-white transition hover:bg-orange-700 active:scale-95">
                        <Send className="h-5 w-5 ml-1" />
                    </button>
                </div>
            </div>
        </div>
    );
}