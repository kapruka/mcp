// lib/mcp.ts

export class KaprukaMCPClient {
  private endpoint: string;
  private sessionId: string | null = null;
  private initPromise: Promise<void> | null = null;

  constructor() {
    const url = process.env.KAPRUKA_MCP_URL;
    if (!url) {
      throw new Error("Missing KAPRUKA_MCP_URL in .env.local");
    }
    this.endpoint = url;
  }

  private async initializeSession() {
    const r1 = await fetch(this.endpoint);
    const sessionId = r1.headers.get("mcp-session-id");
    if (!sessionId) throw new Error("Failed to acquire Kapruka MCP session ID");
    this.sessionId = sessionId;

    const headers = {
      "Content-Type": "application/json",
      "Accept": "application/json, text/event-stream",
      "mcp-session-id": this.sessionId,
    };


    await fetch(this.endpoint, {
      method: "POST",
      headers,
      body: JSON.stringify({
        jsonrpc: "2.0",
        id: 1,
        method: "initialize",
        params: {
          protocolVersion: "2024-11-05",
          capabilities: {},
          clientInfo: { name: "ayubowan-client", version: "1.0.0" }
        }
      })
    });


    await fetch(this.endpoint, {
      method: "POST",
      headers,
      body: JSON.stringify({
        jsonrpc: "2.0",
        method: "notifications/initialized"
      })
    });
  }


  async callTool(toolName: string, args: Record<string, any> = {}) {
    if (!this.initPromise) {
      this.initPromise = this.initializeSession();
    }
    await this.initPromise;


    const formattedArgs = { params: args };

    const payload = {
      jsonrpc: "2.0",
      id: Date.now(),
      method: "tools/call",
      params: {
        name: toolName,
        arguments: formattedArgs,
      },
    };

    try {
      const response = await fetch(this.endpoint, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Accept": "application/json, text/event-stream",
          "mcp-session-id": this.sessionId!,
        },
        body: JSON.stringify(payload),
      });

      if (!response.ok) {
        const errorText = await response.text();
        throw new Error(`HTTP Error: ${response.status} - ${errorText}`);
      }


      const rawText = await response.text();
      const dataLine = rawText.split('\n').find(line => line.startsWith('data: '));

      if (!dataLine) {
        throw new Error("Invalid response format from Kapruka MCP: " + rawText);
      }

      const data = JSON.parse(dataLine.replace('data: ', ''));

      if (data.error) {
        throw new Error(`MCP Error: ${data.error.message} - ${data.error.data || ''}`);
      }


      const rawContent = data.result?.content?.[0]?.text || "[]";

      try {

        return JSON.parse(rawContent);
      } catch {
        return rawContent;
      }
    } catch (error) {
      console.error(`Failed to execute ${toolName}:`, error);
      throw error;
    }
  }
}


export const mcpClient = new KaprukaMCPClient();