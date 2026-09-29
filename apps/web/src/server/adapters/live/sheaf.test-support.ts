import http2 from "node:http2";
import type { AddressInfo } from "node:net";
import { toBinary } from "@bufbuild/protobuf";
import type { Code } from "@connectrpc/connect";
import { PublishResponseSchema } from "@/models/sheaf";

// A gRPC server that answers a stream-in call the way grpc-core does when the handler returns before
// the request stream ends: the status in the trailers, then RST_STREAM(NO_ERROR) for the half of the
// stream the client was still writing (RFC 9113 §8.1). The sheaf service answers so for a publish
// that already landed, lost its race, or is refused on its intent alone.

export interface EarlyAnswer {
  /** When to answer: as soon as the call's headers arrive, or once its first message has. */
  after: "headers" | "firstMessage";
  code: Code | 0;
  message?: string;
  /** A `PublishResponse` to send before an OK status. */
  generation?: bigint;
}

export interface EarlyServer {
  url: string;
  /** Request bytes the server read before the stream closed. */
  received(): number;
  close(): Promise<void>;
}

export async function earlyAnsweringServer(
  answer: EarlyAnswer,
): Promise<EarlyServer> {
  let received = 0;
  const server = http2.createServer();
  const sessions = new Set<http2.ServerHttp2Session>();
  server.on("session", (session) => {
    sessions.add(session);
    session.on("close", () => sessions.delete(session));
  });
  server.on("stream", (stream) => {
    let head = Buffer.alloc(0);
    let answered = false;
    const respond = () => {
      if (answered) return;
      answered = true;
      stream.respond(
        { ":status": 200, "content-type": "application/grpc" },
        { waitForTrailers: true },
      );
      stream.on("wantTrailers", () => {
        stream.sendTrailers({
          "grpc-status": String(answer.code),
          ...(answer.message ? { "grpc-message": answer.message } : {}),
        });
        stream.close(http2.constants.NGHTTP2_NO_ERROR);
      });
      if (answer.generation !== undefined) {
        const body = toBinary(PublishResponseSchema, {
          $typeName: "themis.rpc.sheaf.PublishResponse",
          generation: answer.generation,
        });
        const frame = new Uint8Array(5 + body.length);
        new DataView(frame.buffer).setUint32(1, body.length);
        frame.set(body, 5);
        stream.write(frame);
      }
      stream.end();
    };
    stream.on("data", (chunk: Buffer) => {
      received += chunk.length;
      if (answer.after !== "firstMessage" || answered) return;
      head = Buffer.concat([head, chunk]);
      if (head.length >= 5 && head.length >= 5 + head.readUInt32BE(1)) {
        respond();
      }
    });
    stream.on("error", () => {});
    if (answer.after === "headers") respond();
  });
  await new Promise<void>((resolve) =>
    server.listen(0, "127.0.0.1", () => resolve()),
  );
  const { port } = server.address() as AddressInfo;
  return {
    url: `http://127.0.0.1:${port}`,
    received: () => received,
    close: () =>
      new Promise((resolve) => {
        for (const session of sessions) session.destroy();
        server.close(() => resolve());
      }),
  };
}
