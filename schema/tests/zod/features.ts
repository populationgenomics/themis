import { z } from "zod";

export const colour = z.enum(["red", "green", "blue"]);

export const enumHolder = z.object({
  colour: colour,
});

export const optionalHolder = z.object({
  required_field: z.string(),
  optional_field: z.string().optional(),
});

export const defaultHolder = z.object({
  flagged: z.boolean().optional().default(false),
});

export const inner = z.object({
  value: z.string(),
});

export const outer = z.object({
  inner: inner,
});

export const arrayHolder = z.object({
  tags: z.array(z.string()),
  palette: z.array(colour),
});

export const scalarHolder = z.object({
  count: z.number().int().gte(-2147483648).lte(2147483647),
  ratio: z.number(),
  when: z.iso.datetime(),
  link: z.string().url(),
});
