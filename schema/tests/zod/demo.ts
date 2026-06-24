import { z } from "zod";

export const colour = z.enum(["red", "green", "blue"]);

export const widget = z.object({
  name: z.string(),
  colour: colour,
});

export const catalogue = z.object({
  widgets: z.array(widget),
});
