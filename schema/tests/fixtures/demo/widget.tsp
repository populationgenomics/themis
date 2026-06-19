import "./common.tsp";

// References Colour from common.tsp — one cross-file ref.
model Widget {
  name: string;
  colour: Colour;
}
