// Vercel serverless proxy: the ONLY place the monitor token lives in
// the Vercel deployment — the browser never sees it. Point this at the
// CLOUD delivery store (a Vercel function cannot reach localhost).
//
// Vercel project env vars:
//   DELIVERY_STORE_URL  e.g. https://delivery-store-….run.app/mcp (or base)
//   MCP_TOKEN_MONITOR   the store's monitor role token
//   GITHUB_REPO         optional, e.g. amir707/candidate-app-2 (PR links)
//
// NOTE: a store deployed behind Cloud Run IAM (--no-allow-unauthenticated,
// runbook §9.4) is NOT reachable from Vercel — this function has no
// Google identity. Either keep a public store for the showcase
// dashboard, or host the dashboard on Cloud Run behind the same SA.

export default async function handler(req, res) {
  const raw = process.env.DELIVERY_STORE_URL;
  if (!raw || !process.env.MCP_TOKEN_MONITOR) {
    res.status(500).json({
      error: "DELIVERY_STORE_URL and MCP_TOKEN_MONITOR must be set " +
             "in the Vercel project environment" });
    return;
  }
  const base = raw.replace(/\/mcp\/?$/, "");
  try {
    const upstream = await fetch(`${base}/state`, {
      headers: { "X-Store-Token": process.env.MCP_TOKEN_MONITOR },
    });
    if (!upstream.ok) {
      res.status(upstream.status).json({ error: `store ${upstream.status}` });
      return;
    }
    const state = await upstream.json();
    if (process.env.GITHUB_REPO) state.repo = process.env.GITHUB_REPO;
    res.setHeader("Cache-Control", "no-store");
    res.status(200).json(state);
  } catch (err) {
    res.status(502).json({ error: `store unreachable: ${err.message}` });
  }
}
