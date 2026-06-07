"use client";

import { useState, useRef, useEffect } from 'react';
import { Send, Loader2 } from 'lucide-react';
import MessageRenderer from './MessageRenderer';

interface Message {
    role: 'user' | 'assistant';
    content: string;
}

export default function ChatInterface() {
    const [input, setInput] = useState('');
    const [messages, setMessages] = useState<Message[]>([
        { role: 'assistant', content: 'ආයුබෝවන්! 🌺 Welcome to Kapruka. What are you looking to buy today?' }
    ]);
    const [isLoading, setIsLoading] = useState(false);
    const [isThinking, setIsThinking] = useState(false);
    const [activeTools, setActiveTools] = useState<string[]>([]);
    const messagesEndRef = useRef<HTMLDivElement>(null);

    const scrollToBottom = () => {
        messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
    };

    useEffect(() => {
        scrollToBottom();
    }, [messages, isThinking, activeTools]);

    const handleSend = async () => {
        if (!input.trim() || isLoading) return;

        const userMsg: Message = { role: 'user', content: input };
        const newMessages = [...messages, userMsg];

        setMessages(newMessages);
        setInput('');
        setIsLoading(true);
        setIsThinking(true);
        setActiveTools([]);

        try {
            const res = await fetch('/api/chat', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ messages: newMessages })
            });

            if (!res.body) throw new Error('No body');

            const reader = res.body.getReader();
            const decoder = new TextDecoder();
            let assistantMessage = '';

            while (true) {
                const { done, value } = await reader.read();
                if (done) break;

                const chunk = decoder.decode(value);
                const lines = chunk.split('\n');

                for (const line of lines) {
                    if (line.startsWith('data: ')) {
                        const dataStr = line.replace('data: ', '').trim();

                        if (dataStr === '[DONE]') {
                            break;
                        }

                        try {
                            const data = JSON.parse(dataStr);

                            if (data.type === 'tool_activity') {
                                setIsThinking(false);
                                setActiveTools(data.tools);
                            } else if (data.type === 'text') {
                                setIsThinking(false);
                                setActiveTools([]);
                                assistantMessage += data.content;
                                setMessages([...newMessages, { role: 'assistant', content: assistantMessage }]);
                            } else if (data.type === 'error') {
                                setIsThinking(false);
                                setActiveTools([]);
                                setMessages([...newMessages, { role: 'assistant', content: data.content }]);
                            }
                        } catch (e) {
                        }
                    }
                }
            }
        } catch (error) {
            setIsThinking(false);
            setMessages([...newMessages, { role: 'assistant', content: 'Connection error. Please try again.' }]);
        } finally {
            setIsLoading(false);
            setIsThinking(false);
            setActiveTools([]);
        }
    };

    return (
        <div className="mx-auto flex h-full max-w-4xl flex-col bg-white shadow-lg sm:rounded-lg sm:border sm:border-gray-200 sm:mt-4 sm:h-[calc(100vh-6rem)]">
            <div className="flex-1 overflow-y-auto p-4 sm:p-6 space-y-6">
                {messages.map((m, i) => (
                    <div key={i} className={`flex ${m.role === 'user' ? 'justify-end' : 'justify-start'}`}>
                        <div className={`max-w-[85%] rounded-2xl p-4 shadow-sm ${m.role === 'user' ? 'bg-orange-600 text-white rounded-tr-sm' : 'bg-orange-50 text-gray-800 border border-orange-100 rounded-tl-sm'}`}>
                            {m.role === 'user' ? m.content : <MessageRenderer content={m.content} />}
                        </div>
                    </div>
                ))}

                {(isThinking || activeTools.length > 0) && (
                    <div className="flex justify-start">
                        <div className="flex items-center gap-2 max-w-[85%] rounded-2xl rounded-tl-sm bg-gray-50 p-3 text-gray-500 border border-gray-200 text-sm">
                            <Loader2 className="h-4 w-4 animate-spin" />
                            {activeTools.length > 0 ? 'Searching Kapruka catalog...' : 'Thinking...'}
                        </div>
                    </div>
                )}
                <div ref={messagesEndRef} />
            </div>

            <div className="border-t bg-gray-50 p-4 sm:rounded-b-lg">
                <div className="flex items-center gap-2 rounded-full border border-gray-300 bg-white px-4 py-2 shadow-sm focus-within:border-orange-500 focus-within:ring-1 focus-within:ring-orange-500 transition-all">
                    <input
                        type="text"
                        value={input}
                        onChange={(e) => setInput(e.target.value)}
                        onKeyDown={(e) => e.key === 'Enter' && handleSend()}
                        placeholder="Type your message..."
                        className="flex-1 bg-transparent outline-none text-gray-700"
                        disabled={isLoading}
                    />
                    <button
                        onClick={handleSend}
                        disabled={isLoading || !input.trim()}
                        className="flex h-10 w-10 items-center justify-center rounded-full bg-orange-600 text-white transition hover:bg-orange-700 active:scale-95 disabled:opacity-50 disabled:hover:bg-orange-600"
                    >
                        <Send className="h-5 w-5 ml-1" />
                    </button>
                </div>
            </div>
        </div>
    );
}