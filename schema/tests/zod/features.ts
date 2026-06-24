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

export const literalHolder = z.object({
  kind: z.literal("widget"),
});

export const freeToRead = z.object({
  access: z.literal("free-to-read"),
});

export const licensed = z.object({
  access: z.literal("licensed"),
  publisher: z.string(),
});

export const access = z.union([freeToRead, licensed]);

export const accessHolder = z.object({
  access: access,
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
  when: z.coerce.date(),
  day: z.coerce.date(),
  link: z.string().url(),
});
