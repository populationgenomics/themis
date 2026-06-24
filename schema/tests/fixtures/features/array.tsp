import "./common.tsp";

// Array fields: a scalar array and an enum array. Emit {type:array, items:...};
// Pydantic list[X]; Zod z.array(...).
model ArrayHolder {
  tags: string[];
  palette: Colour[];
}
