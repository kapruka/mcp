// app/api/test/route.ts
import { NextResponse } from "next/server";
import { mcpClient } from "@/lib/mcp";

export async function GET() {
    try {

        const categories = await mcpClient.callTool("kapruka_list_categories", { depth: 1 });

        const searchResults = await mcpClient.callTool("kapruka_search_products", {
            q: "cake",
            limit: 3
        });

        return NextResponse.json({
            status: "Success! Day 1 Complete.",
            categories: categories,
            firstThreeCakes: searchResults
        });

    } catch (error: any) {
        return NextResponse.json(
            { status: "Failed", error: error.message },
            { status: 500 }
        );
    }
}