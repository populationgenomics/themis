import "@typespec/protobuf";

using TypeSpec.Protobuf;

/**
 * The `hello` service: a trivial session-token-authed internal service. `sayHello` resolves the
 * request's session token to its Project + Analysis binding and echoes the caller's note against it.
 */
@package({ name: "themis.rpc.hello" })
namespace Hello;

/** The caller's note, echoed back in the greeting. */
model SayHelloRequest {
  @field(1) note: string;
}

/**
 * The greeting plus the binding the caller's session token resolved to — evidence the injected token
 * authorized the call and reached the intended Analysis.
 */
model SayHelloResponse {
  @field(1) greeting: string;
  @field(2) project_id: string;
  @field(3) analysis_id: string;
}

/** The hello service: echo the caller's note against its resolved session binding. */
@TypeSpec.Protobuf.service
interface Hello {
  /** Resolve the request's session token and echo the note against the bound Analysis. */
  sayHello(...SayHelloRequest): SayHelloResponse;
}
