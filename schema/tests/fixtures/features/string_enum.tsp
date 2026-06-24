import "./common.tsp";

// String enum reference: emits {type:string, enum:[...]}; Pydantic StrEnum;
// Zod z.enum. Colour lives in common.tsp (cross-file ref).
model EnumHolder {
  colour: Colour;
}
