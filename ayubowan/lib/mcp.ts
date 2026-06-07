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
    // 1. Get Session ID from the Kapruka server
    const r1 = await fetch(this.endpoint);
    const sessionId = r1.headers.get("mcp-session-id");
    if (!sessionId) throw new Error("Failed to acquire Kapruka MCP session ID");
    this.sessionId = sessionId;

    const headers = {
      "Content-Type": "application/json",
      "Accept": "application/json, text/event-stream",
      "mcp-session-id": this.sessionId,
    };

    // 2. Initialize Request (Required by MCP Protocol)
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

    // 3. Initialized Notification
    await fetch(this.endpoint, {
      method: "POST",
      headers,
      body: JSON.stringify({
        jsonrpc: "2.0",
        method: "notifications/initialized"
      })
    });
  }

  // Universal method to call any tool on the Kapruka MCP Server
  async callTool(toolName: string, args: Record<string, any> = {}) {
    if (!this.initPromise) {
      this.initPromise = this.initializeSession();
    }
    await this.initPromise;

    // FastMCP wraps all arguments under a single "params" key for Pydantic models
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

      // Kapruka's streamable HTTP transport returns SSE format (event: message \n data: {...})
      const rawText = await response.text();
      const dataLine = rawText.split('\n').find(line => line.startsWith('data: '));
      
      if (!dataLine) {
        throw new Error("Invalid response format from Kapruka MCP: " + rawText);
      }

      const data = JSON.parse(dataLine.replace('data: ', ''));

      if (data.error) {
        throw new Error(`MCP Error: ${data.error.message} - ${data.error.data || ''}`);
      }

      // Extract the raw text from the MCP content block
      const rawContent = data.result?.content?.[0]?.text || "[]";
      
      try {
        // Most Kapruka tools return JSON strings, so we parse it into a real object
        return JSON.parse(rawContent);
      } catch {
        return rawContent; // Fallback if it returns plain text
      }
    } catch (error) {
      console.error(`Failed to execute ${toolName}:`, error);
      throw error;
    }
  }
}

// Export a single instance to use across your app
export const mcpClient = new KaprukaMCPClient();