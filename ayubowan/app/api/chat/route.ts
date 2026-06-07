// app/api/chat/route.ts
import OpenAI from 'openai';
import { KAPRUKA_TOOLS } from '@/agents/tools';
import { ORCHESTRATOR_SYSTEM_PROMPT } from '@/agents/orchestrator';
import { mcpClient } from '@/lib/mcp';

const client = new OpenAI({
    baseURL: "https://openrouter.ai/api/v1",
    apiKey: process.env.OPENROUTER_API_KEY,
});

export const maxDuration = 60;

export async function POST(req: Request) {
    const { messages, model } = await req.json();


    let currentMessages: OpenAI.ChatCompletionMessageParam[] = [
        { role: 'system', content: ORCHESTRATOR_SYSTEM_PROMPT },
        ...messages
    ];

    const encoder = new TextEncoder();
    const stream = new ReadableStream({
        async start(controller) {
            try {
                let iterations = 0;
                const MAX_ITERATIONS = 5;

                while (iterations < MAX_ITERATIONS) {
                    iterations++;


                    const response = await client.chat.completions.create({
                        model: model || 'openrouter/free',
                        messages: currentMessages,
                        tools: KAPRUKA_TOOLS,
                        tool_choice: 'auto',
                    });

                    const message = response.choices[0].message;


                    if (message.tool_calls && message.tool_calls.length > 0) {


                        const toolNames: string[] = [];
                        for (const t of message.tool_calls) {
                            if (t.type === 'function') {
                                toolNames.push(t.function.name);
                            }
                        }

                        controller.enqueue(encoder.encode(
                            `data: ${JSON.stringify({ type: 'tool_activity', tools: toolNames })}\n\n`
                        ));


                        currentMessages.push(message);


                        for (const toolCall of message.tool_calls) {
                            if (toolCall.type !== 'function') continue;

                            const args = JSON.parse(toolCall.function.arguments);


                            const toolResult = await mcpClient.callTool(toolCall.function.name, args);


                            currentMessages.push({
                                role: 'tool',
                                tool_call_id: toolCall.id,
                                content: typeof toolResult === 'string' ? toolResult : JSON.stringify(toolResult)
                            });
                        }


                        continue;
                    }


                    if (message.content) {
                        controller.enqueue(encoder.encode(
                            `data: ${JSON.stringify({ type: 'text', content: message.content })}\n\n`
                        ));
                    }

                    break;
                }
            } catch (err) {
                console.error('Agent error:', err);
                controller.enqueue(encoder.encode(
                    `data: ${JSON.stringify({ type: 'error', content: 'Something went wrong. Please try again.' })}\n\n`
                ));
            } finally {
                controller.enqueue(encoder.encode('data: [DONE]\n\n'));
                controller.close();
            }
        }
    });

    return new Response(stream, {
        headers: {
            'Content-Type': 'text/event-stream',
            'Cache-Control': 'no-cache',
            'Connection': 'keep-alive',
        }
    });
}