import { isAuthenticated } from "./api/[[path]]";

const MAX_STANDARD_BODY = 262144; // 256 KiB
const MAX_UPLOAD_BODY = 99614720; // 95 MiB (safely under Cloudflare Free 100 MB request ceiling)

export const onRequest: PagesFunction<any> = async (context) => {
  const { request, env } = context;
  const url = new URL(request.url);
  const path = url.pathname;

  const origin = request.headers.get("Origin");
  if (!["GET", "HEAD", "OPTIONS"].includes(request.method) && origin && origin !== url.origin) {
    return Response.json({ error: "Cross-origin requests are not allowed." }, { status: 403 });
  }

  const isBgUpload = path === "/api/backgrounds/upload" && request.method === "POST";
  const contentLen = Number(request.headers.get("Content-Length") || 0);

  if (isBgUpload) {
    if (contentLen > MAX_UPLOAD_BODY) {
      return Response.json(
        {
          error:
            "File is over StoryForge's direct-upload limit (95 MiB). Use URL / Google Drive import instead.",
        },
        { status: 413 }
      );
    }
  } else {
    if (contentLen > MAX_STANDARD_BODY) {
      return Response.json({ error: "Request exceeds 256 KiB." }, { status: 413 });
    }
    if (path.startsWith("/api/") && !["GET", "HEAD"].includes(request.method)) {
      const bytes = await request.clone().arrayBuffer();
      if (bytes.byteLength > MAX_STANDARD_BODY) {
        return Response.json({ error: "Request exceeds 256 KiB." }, { status: 413 });
      }
    }
  }

  if ((path.startsWith("/samples/") || path === "/voices.json") && !(await isAuthenticated(request, env))) {
    return Response.json({ error: "Authentication required" }, { status: 401 });
  }

  let response: Response;
  try {
    response = await context.next();
  } catch {
    response = Response.json(
      { error: "Request could not be processed. Check the supplied data and retry." },
      { status: 500 }
    );
  }

  const secure = new Response(response.body, response);
  secure.headers.set("X-Content-Type-Options", "nosniff");
  secure.headers.set("Referrer-Policy", "same-origin");
  secure.headers.set("X-Frame-Options", "DENY");
  secure.headers.set(
    "Content-Security-Policy",
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; media-src 'self' blob:; img-src 'self' data: blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
  );
  if (path.startsWith("/api/") || path.startsWith("/samples/") || path === "/voices.json") {
    secure.headers.set("Cache-Control", "private, no-store");
  }
  return secure;
};
