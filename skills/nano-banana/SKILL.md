---
name: nano-banana
description: Generate and edit images using Google Gemini. Use when asked to create, generate, edit, or modify any image, artwork, photo, or visual content.
---

> **Not for engineering/hardware renders.** For a 3D view of a real part or device, use the `cad` skill (real geometry).


# Nano Banana Pro Image Generation

Generate images from text prompts or edit existing images using Google's Nano Banana Pro model (Gemini 3 Pro Image).

## Generate an Image

```bash
~/.claude/skills/nano-banana/scripts/nano-banana "your prompt here" -o /tmp/output.png
```

## Edit an Existing Image

```bash
~/.claude/skills/nano-banana/scripts/nano-banana "make the sky purple" -i /path/to/input.jpg -o /tmp/edited.png
```

## Options

| Flag | Description |
|------|-------------|
| `-o, --output` | Output file path (default: generated_image.png) |
| `-i, --input` | Input image for editing (optional) |
| `--model` | Model choice: `gemini-3-pro-image-preview` (default, best quality) or `gemini-2.5-flash-image` (faster) |

## Examples

```bash
# Generate a logo
~/.claude/skills/nano-banana/scripts/nano-banana "minimalist logo for a coffee shop called 'Bean There'" -o /tmp/logo.png

# Edit a photo
~/.claude/skills/nano-banana/scripts/nano-banana "remove the background and make it transparent" -i photo.jpg -o /tmp/edited.png

# Quick generation with faster model
~/.claude/skills/nano-banana/scripts/nano-banana "sunset over mountains" -o /tmp/sunset.png --model gemini-2.5-flash-image
```

## Sending Generated Images

After generating, send to a contact:
```bash
~/.claude/skills/sms-assistant/scripts/send-sms "+phone" --image /tmp/output.png

# Or with a caption
~/.claude/skills/sms-assistant/scripts/send-sms "+phone" "Here's what I generated!" --image /tmp/output.png
```

## Notes

- API key is loaded from `~/.claude/secrets.env` (GEMINI_API_KEY)
- Pro model has better quality but takes 5-15 seconds (thinking mode)
- Flash model is faster for simpler generations
- Supported input formats: JPEG, PNG, WebP
- Output is always PNG

## Batch Generation Pattern

For generating multiple images in parallel (e.g. 10-12 images on a theme):

### 1. Parallel background generation
```bash
# Generate all images in parallel, zero-padded names
for i in $(seq -w 1 12); do
  ~/.claude/skills/nano-banana/scripts/nano-banana "your prompt $i" -o /tmp/topic-$i.png &
done
wait  # barrier — wait for all to finish
```

### 2. Sequential send loop
```bash
# Send each image with a numbered caption
for i in $(seq -w 1 12); do
  ~/.claude/skills/sms-assistant/scripts/reply "Caption for image $i" --image /tmp/topic-$i.png
done
```

**Key rules:**
- Zero-pad filenames: `/tmp/<topic>-01.png`, `/tmp/<topic>-02.png`, etc.
- Always use `wait` barrier after parallel generation before sending
- Send sequentially (one at a time) to preserve order and avoid message flooding
- Use numbered captions to give context per image

---

# Prompting Techniques

## Core Principles

### 1. Describe Scenes, Don't List Keywords
Write narrative, descriptive paragraphs rather than keyword lists. Gemini excels at understanding natural language.

**Bad:** `woman, red dress, park, running, sunny`

**Good:** `A young woman in a flowing red dress running through a sunlit park, her hair streaming behind her`

### 2. The S.C.A.L.E. Method
For professional results, structure your prompts using:

- **S**ubject: Be specific about the person, expression, features
- **C**omposition: Define camera angle (low angle, 85mm portrait, cinematic wide-shot)
- **A**ction/Atmosphere: Define the "vibe" (golden hour, misty morning, neon rain)
- **L**ighting: Use professional terms (rim lighting, Rembrandt shadows, volumetric fog)
- **E**dit Limits: Include "Keep the face exactly the same" when preserving identity

### 3. Use Photography Language
Control composition with technical terms:
- Camera angles: wide-angle shot, macro shot, low-angle perspective
- Lens specs: 85mm f/1.4 lens, 50mm portrait lens
- Lighting: three-point lighting, soft fill at 30% intensity, rim light
- Film references: Kodak Portra 400, Sony A7III look

### 4. Skip the Keyword Spam
You don't need "4k, trending on artstation, masterpiece, highly detailed" anymore. Gemini understands natural language.

---

## Prompt Templates

### Photorealistic Scenes
```
A photorealistic [shot type] of [subject], [action or expression], set in [environment].
The scene is illuminated by [lighting description], creating a [mood] atmosphere.
Captured with a [camera/lens details], emphasizing [key textures and details].
The image should be in a [aspect ratio] format.
```

### Style Transfer
```
Transform the provided photograph of [subject] into the artistic style of [artist/art style].
Preserve original composition but render with [stylistic elements description].
```

### Product Photography
```
A high-resolution, studio-lit product photograph of [product] on a [surface].
Lighting is a [setup] to [purpose]. Camera angle is a [angle type].
Ultra-realistic, sharp focus on [detail]. [Aspect ratio].
```

---

## Copy-Paste Prompts by Category

### Portrait Enhancement

**Professional Photo Quality:**
```
Transform this photo into a professional photograph. Dramatically improve image clarity,
sharpness, colors, and lighting. Add slight bokeh blur to the background. Enhance skin
tones and reduce any noise or artifacts. Keep all people exactly the same.
```

**Natural Skin Retouching:**
```
Perform natural skin retouching on the subject, smoothing only minor texture while keeping
pores and natural highlights visible. Remove tiny blemishes without touching face shape or
altering skin color. Keep eye and hair detail sharp.
```

**Professional Headshot:**
```
Enhance this portrait with professional studio lighting. Add soft, diffused key light from
the front with subtle fill lighting to eliminate harsh shadows. Replace the background with
a neutral, slightly blurred studio backdrop in light gray. Maintain the subject's original
facial features and expressions.
```

### Lighting Enhancement

**Golden Hour Transformation:**
```
Transform this image to appear as if photographed during golden hour. Apply warm, soft
lighting with a color temperature around 3500 to 4000 Kelvin. Add long, gentle shadows
that create depth and dimension.
```

**Portrait Lighting Enhancement:**
```
Enhance portrait lighting to mimic soft studio light from camera left. Lift shadows slightly,
add midtone contrast, keep highlights natural, and maintain realistic skin tone.
```

### Color Grading

**Cinematic Color Grading:**
```
Apply cinematic color grading with teal shadows and warm highlights at moderate strength.
Keep skin tones natural, increase midtone clarity, and add light film grain.
```

**Vintage Film Look:**
```
Turn this photo into a vintage 90s film look with grain, muted tones, and slight green tint.
```

### Background Editing

**Background Blur/Bokeh:**
```
Enhance the depth of field in this image to create a shallow focus effect. Keep the subject
in sharp focus while progressively blurring the background to create smooth bokeh.
```

**Background Replacement:**
```
Replace the background of this portrait with [desired background description].
Keep the subject sharp and properly lit. Adjust shadows and lighting to ensure
the subject stands out clearly.
```

### Style Transfers

**Comic Book Style:**
```
Transform this photo into a vibrant comic book illustration style. Use bold black outlines,
halftone dot patterns, dynamic colors with high saturation, and dramatic comic book shading.
Keep all the people exactly as they are - same faces, positions, and expressions.
```

**Studio Ghibli Style:**
```
Reimagine this photo as a Studio Ghibli-inspired illustration, using soft pastel tones,
gentle lighting, and hand-painted textures. Preserve the original composition and likeness.
```

**Watercolor Painting:**
```
Transform the portrait into a watercolor painting with soft brush textures,
preserving facial features and key details.
```

### Object Operations

**Object Removal:**
```
Remove [element to remove] from this image. Fill the background naturally with matching
texture and lighting. Avoid changes to other elements.
```

**Add Object:**
```
Add [desired element] to this image. Place it naturally in the scene, matching the
lighting, perspective, and style of the original.
```

---

## Advanced Techniques

### Using ALL CAPS for Emphasis
Capitalizing important requirements increases compliance:
```
Create a portrait. KEEP THE FACE EXACTLY AS IN THE ORIGINAL. Do not modify facial features.
```

### Markdown/Structured Formatting
Using dashed lists helps the model parse complex rules:
```
Edit this portrait with the following requirements:
- Smooth minor skin texture only
- Preserve pores and natural highlights
- Remove tiny blemishes
- Keep eye detail sharp
- Do not alter face shape or skin color
```

### JSON Structure for Complex Prompts
For very detailed specifications:
```json
{
  "subject": "woman, mid-30s, professional attire",
  "lighting": "soft key light from upper left, fill from right at 30%",
  "background": "blurred office environment",
  "style": "corporate headshot, editorial quality"
}
```

### Hex Color Codes for Precision
Instead of "blue background," use:
```
Background color #1E3A5F (deep navy blue)
```

### Semantic Negative Prompts
Instead of saying what you DON'T want, describe what you DO want:

**Bad:** `no cars in the image`
**Good:** `an empty, deserted street with no signs of traffic`

### The Collage Method
Merge multiple reference images into a single input and prompt it. Works well for combining styles or maintaining character consistency across images.

### Quantifying Adjustments
Be specific with measurements:
- "Whiten teeth by 5%"
- "Boost saturation by about 6 percent"
- "Color temperature around 3500 to 4000 Kelvin"

---

## Preserving Identity During Edits

Always include these phrases when transforming portraits:
- "Keep the same person"
- "Preserve facial features"
- "Keep facial features exactly consistent"
- "Do not change the face"
- "KEEP THE FACE EXACTLY AS IN THE ORIGINAL"

---

## Avoiding Common Problems

### Plastic-Looking Skin
**Remove these words:** smooth, flawless, perfect

**Add these phrases:**
- "natural skin texture with visible pores"
- "fine lines, realistic detail, not airbrushed"
- "retain pores"
- "no plastic skin"

### Minimal Changes
If edits seem too subtle, use more direct language:
- "Dramatically improve"
- "Significant enhancement"
- "Transform this into"
- Explicitly name the effects you want (e.g., "Add bokeh blur to the background")

### Distorted Faces
Add constraints:
```
no distorted face, no unnatural pose, no dead eyes
```

---

## Iterative Refinement

For best results, solve one problem at a time:
1. First fix composition/framing
2. Then adjust lighting
3. Then correct color
4. Finally add stylistic effects

Use multi-turn conversation to progressively refine:
- "Make the lighting warmer"
- "Add more bokeh to the background"
- "Make the colors more vibrant"

---

## Quick Reference Checklist

1. **Subject**: Who/what is the main focus
2. **Action**: What they're doing
3. **Location/Context**: Where the scene takes place
4. **Composition**: Camera angle, framing, aspect ratio
5. **Lighting**: Direction, quality, color temperature
6. **Style**: Photorealistic, cinematic, illustrated, vintage
7. **Edit Instructions**: Specific changes (retouch level, color correction)
8. **Guardrails**: What to preserve, what NOT to change

---

## Aspect Ratios

| Ratio | Best For |
|-------|----------|
| 1:1 (square) | Social media posts, profile pictures |
| 4:3 (fullscreen) | Film/photography, classic photos |
| 3:4 (portrait) | Vertical scenes, portraits |
| 16:9 (widescreen) | Landscapes, backgrounds, YouTube |
| 9:16 (tall portrait) | Stories, tall subjects, buildings |

---

## Text in Images

For generating text within images:
- Keep text under 25 characters for optimal results
- Use two or three phrases maximum
- Specify font style generally: "bold serif," "clean sans-serif," "handwritten"
- Example: `Create a logo with text 'HELLO WORLD' in bold red serif font`

---

## Meta Prompting

Ask Gemini to draft detailed prompts for you rather than writing them directly:
```
Write me a detailed image generation prompt for a professional headshot
of a woman in her 30s in a modern office setting
```
Then use the resulting detailed prompt. This often produces significantly better output.

---

## Explicit Intent Phrases

Always include phrases like these to ensure image generation (not text response):
- "Create an image of..."
- "Generate an image of..."
- "Make a photo of..."

---

## Technical Limits

| Model | Max Input Images |
|-------|------------------|
| gemini-2.5-flash-image | 3 images |
| gemini-3-pro-image-preview | 14 images |

When editing, the model generally preserves the input aspect ratio. If needed, state: "Do not change the input aspect ratio."

---

## Lens Guide for Photorealism

| Subject | Recommended Lens |
|---------|------------------|
| Portraits | 85mm, 50mm |
| Products/Macro | 60-105mm |
| Landscapes | 10-24mm wide-angle |
| Action/Sports | 100-400mm telephoto |
| General/Street | 35mm, 50mm |

Example: `Portrait shot with 85mm f/1.4 lens, soft bokeh background`

---

## Five Action Words for Edits

When editing existing images, use these action words:
1. **Add** - insert new elements ("Add a rainbow in the sky")
2. **Change** - modify existing elements ("Change the shirt color to blue")
3. **Make** - transform qualities ("Make the lighting warmer")
4. **Remove** - delete elements ("Remove the person in the background")
5. **Replace** - swap elements ("Replace the background with a beach scene")

---

## More Copy-Paste Prompts

**Professional Relighting:**
```
Relight the portrait to mimic softbox key at 45 degrees and low-intensity fill,
preserving original catchlights where possible. No hard shadows on the neck.
Keep background separation subtle.
```

**Outdoor Tone Correction:**
```
Correct outdoor tones by reducing green cast slightly, recovering highlight detail,
and warming midtones. Keep sky gradients smooth and preserve tree detail.
```

**3D Figurine/Action Figure:**
```
Create a 1/7 scale commercialized figurine of the character in the picture,
in a realistic style, placed on a display shelf. Include a collectible box
with clear window, bold graphics, and the name on packaging.
```

---

## Pro Tips

1. **Start with a reference photo** - even a casual selfie helps guide facial structure
2. **Include a quality checklist** at the end of prompts so Gemini self-audits
3. **Provide context and intent** - explain the image's purpose (e.g., "for a professional LinkedIn profile")
4. **Edit, don't re-roll** - if an image is 80% correct, use conversational edits rather than regenerating
5. **Use numeric directions** for predictable intensity (e.g., "boost saturation by 6 percent")
