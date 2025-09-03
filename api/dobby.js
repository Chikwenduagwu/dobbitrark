// api/dobby.js
// Vercel Serverless function (ES module). For Netlify use a slightly different signature.

export default async function handler(req, res) {
  if (req.method !== "POST") {
    res.status(405).json({ error: "Method not allowed, POST only" });
    return;
  }

  const FIREWORKS_KEY = process.env.FIREWORKS_API_KEY;
  if (!FIREWORKS_KEY) {
    res.status(500).json({ error: "FIREWORKS_API_KEY not configured in environment" });
    return;
  }

  const FORWARD_URL = "https://api.fireworks.ai/inference/v1/chat/completions";

  try {
    // forward body as-is to the provider
    const r = await fetch(FORWARD_URL, {
      method: "POST",
      headers: {
        "Authorization": `Bearer ${FIREWORKS_KEY}`,
        "Content-Type": "application/json"
      },
      body: JSON.stringify(req.body)
    });

    const text = await r.text(); // raw text
    // pass through status
    res.status(r.status).setHeader("Content-Type", "application/json");
    res.send(text);
  } catch (err) {
    console.error("Proxy error:", err);
    res.status(502).json({ error: "Proxy failed", details: String(err) });
  }
}
