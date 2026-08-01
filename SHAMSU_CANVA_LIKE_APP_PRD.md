# Product Requirements Document: StudioForge

## 1. Product Summary

StudioForge is a Canva-like visual design application for creating social posts, presentations, posters, flyers, thumbnails, simple documents, brand kits, and reusable templates. The product must provide a polished browser-based editor with a real canvas, drag-and-drop objects, text editing, image placement, shape tools, layer controls, template browsing, export flows, and local-first persistence.

This PRD is written to test Shamsu's ability to build a complex creative application, not a static landing page. A weak implementation would only show a marketing page. A strong implementation must build an actual usable design editor as the first screen.

## 2. Goals

The app must allow a user to:

- Create a new design from a preset size or template.
- Add text, shapes, images, icons, and backgrounds.
- Move, resize, rotate, reorder, duplicate, lock, hide, group, and delete objects.
- Edit text font, size, color, alignment, line height, letter spacing, and style.
- Use a brand kit with colors, fonts, and logos.
- Browse reusable templates.
- Export designs as PNG, JPEG, PDF, and JSON.
- Save and reopen projects locally.
- Manage pages in a multi-page design.
- Use keyboard shortcuts for common editor actions.

## 3. Non-Goals

- No cloud account system is required for version 1.
- No real payment or premium asset marketplace.
- No real-time multiplayer collaboration in version 1.
- No generative AI image creation is required.
- No full Photoshop-style raster editing.
- No video timeline editing.

## 4. Recommended Stack

If no existing stack exists, use:

- TypeScript
- React
- Vite
- Canvas rendering through Fabric.js, Konva, or a comparable proven canvas library
- SQLite or IndexedDB/local JSON persistence
- Zod for validation
- Vitest for unit tests
- Playwright for end-to-end tests

Use a proven canvas library for object manipulation. Do not hand-roll all canvas selection, transform handles, hit testing, and object rendering unless the user explicitly asks for from-scratch canvas code.

## 5. Core Product Principles

1. The editor is the product.

The first screen must be the usable design editor or a project dashboard that immediately opens into the editor. Do not build a marketing landing page.

2. Fast creative flow.

Adding text, shapes, images, and templates must be fast and obvious.

3. Local-first durability.

Projects must save locally and reopen without internet.

4. Inspectable state.

Designs should export to JSON so Shamsu and tests can inspect object state.

5. Professional UI.

The app should feel like a serious creative tool: dense, polished, predictable, and responsive.

## 6. User Roles

### Entity: UserProfile

Fields:

- id: string, required, unique
- display_name: string, required
- email: string, optional, valid email
- default_brand_kit_id: string, optional, references BrandKit
- theme_mode: enum, values: system, light, dark
- created_at: datetime
- updated_at: datetime

Rules:

- A default local profile must be created on first launch.
- Email is optional because the app is local-first.

## 7. Core Entities

### Entity: DesignProject

Fields:

- id: string, required, unique
- name: string, required
- description: text, optional
- owner_profile_id: string, required, references UserProfile
- brand_kit_id: string, optional, references BrandKit
- document_type: enum, values: social_post, presentation, poster, flyer, thumbnail, document, custom
- width: number, required
- height: number, required
- unit: enum, values: px, in, mm
- status: enum, values: draft, archived, exported
- thumbnail_data_url: text, optional
- created_at: datetime
- updated_at: datetime
- deleted_at: datetime, nullable

Rules:

- Width and height must be positive.
- Deleted projects are soft-deleted.
- Archived projects are hidden by default.

### Entity: DesignPage

Fields:

- id: string, required, unique
- project_id: string, required, references DesignProject
- page_number: integer, required
- name: string, optional
- width: number, required
- height: number, required
- background_type: enum, values: color, gradient, image, transparent
- background_value: text, optional
- created_at: datetime
- updated_at: datetime

Rules:

- Page numbers must be unique within a project.
- A project must always have at least one page.
- Deleting the last page is not allowed.

### Entity: CanvasObject

Fields:

- id: string, required, unique
- page_id: string, required, references DesignPage
- object_type: enum, values: text, image, shape, icon, line, group, frame, background
- name: string, optional
- x: number, required
- y: number, required
- width: number, required
- height: number, required
- rotation: number, default 0
- opacity: number, default 1
- z_index: integer, required
- locked: boolean, default false
- hidden: boolean, default false
- group_id: string, optional
- style_json: text, required
- content_json: text, required
- created_at: datetime
- updated_at: datetime
- deleted_at: datetime, nullable

Rules:

- Width and height must be positive.
- Opacity must be between 0 and 1.
- Locked objects cannot be moved, resized, edited, or deleted until unlocked.
- Hidden objects do not appear in export.
- z_index controls layer order.
- content_json and style_json must be valid JSON.

### Entity: TextStyle

Fields:

- id: string, required, unique
- project_id: string, optional, references DesignProject
- brand_kit_id: string, optional, references BrandKit
- name: string, required
- font_family: string, required
- font_size: number, required
- font_weight: string, optional
- color: string, required
- alignment: enum, values: left, center, right, justify
- line_height: number, optional
- letter_spacing: number, optional
- created_at: datetime
- updated_at: datetime

Rules:

- Font size must be positive.
- Color must be valid hex, rgb, rgba, hsl, or named safe color.

### Entity: Asset

Fields:

- id: string, required, unique
- name: string, required
- asset_type: enum, values: image, icon, logo, pattern, texture, font, template_preview
- source_type: enum, values: local_upload, built_in, generated, external_reference
- mime_type: string, required
- size_bytes: integer, optional
- width: number, optional
- height: number, optional
- data_url: text, optional
- file_path: text, optional
- tags: string array
- created_at: datetime
- updated_at: datetime
- deleted_at: datetime, nullable

Rules:

- Asset must have either data_url or file_path.
- Deleted assets remain available to existing projects until purged.
- Unsupported mime types must be rejected.

### Entity: Template

Fields:

- id: string, required, unique
- name: string, required
- category: enum, values: social, presentation, marketing, business, education, personal, custom
- width: number, required
- height: number, required
- preview_asset_id: string, optional, references Asset
- project_json: text, required
- tags: string array
- premium: boolean, default false
- created_at: datetime
- updated_at: datetime

Rules:

- project_json must be valid design JSON.
- Premium templates may exist but must be usable locally in version 1 without payment.

### Entity: BrandKit

Fields:

- id: string, required, unique
- name: string, required
- colors_json: text, required
- fonts_json: text, required
- logo_asset_ids: string array
- created_at: datetime
- updated_at: datetime
- deleted_at: datetime, nullable

Rules:

- colors_json and fonts_json must be valid JSON.
- Brand kits can be applied to templates and existing projects.

### Entity: ExportJob

Fields:

- id: string, required, unique
- project_id: string, required, references DesignProject
- page_ids: string array
- format: enum, values: png, jpeg, pdf, json
- status: enum, values: queued, running, completed, failed
- output_path: text, optional
- error_message: text, optional
- created_at: datetime
- completed_at: datetime, optional

Rules:

- Export job must record failure reason when failed.
- JSON export must preserve all design state.

### Entity: ProjectHistoryEvent

Fields:

- id: string, required, unique
- project_id: string, required, references DesignProject
- actor_profile_id: string, optional, references UserProfile
- event_type: string, required
- summary: string, required
- before_json: text, optional
- after_json: text, optional
- created_at: datetime

Rules:

- Create, update, duplicate, delete, export, import, template apply, and brand kit apply events must be logged.
- History events are append-only.

## 8. Required Editor Features

### Canvas Workspace

Must include:

- Center canvas area.
- Page artboard with visible boundary.
- Zoom controls.
- Pan controls.
- Rulers or alignment guides if practical.
- Selection handles.
- Multi-select.
- Snap-to-center and snap-to-edge guides.
- Safe area toggle.

Acceptance criteria:

- User can select an object.
- User can drag object.
- User can resize object.
- User can rotate object.
- User can zoom in and out.
- Canvas state updates without page reload.

### Object Toolbar

Must include controls for:

- Position x and y.
- Size width and height.
- Rotation.
- Opacity.
- Fill color.
- Stroke color.
- Stroke width.
- Lock/unlock.
- Hide/show.
- Duplicate.
- Delete.
- Bring forward.
- Send backward.

### Text Editing

Must support:

- Add heading.
- Add subheading.
- Add body text.
- Edit text inline or in a side panel.
- Font family.
- Font size.
- Bold.
- Italic.
- Underline.
- Text color.
- Alignment.
- Line height.
- Letter spacing.
- Text box resize.

Acceptance criteria:

- User can double-click text and edit content.
- Text style persists after saving and reopening.
- Empty text object is either prevented or removed on blur.

### Shape Tools

Must support:

- Rectangle.
- Circle.
- Triangle.
- Line.
- Arrow.
- Star.
- Rounded rectangle.

Acceptance criteria:

- Shapes can be added from toolbar.
- Shapes can be recolored.
- Shapes can be resized and rotated.

### Image Tools

Must support:

- Upload local image.
- Add image to canvas.
- Crop or fit image into frame.
- Replace image.
- Apply opacity.
- Basic filters: grayscale, blur, brightness, contrast if practical.

Acceptance criteria:

- Uploaded image appears in asset library.
- Image persists in project save.
- Export includes image.

### Pages

Must support:

- Add page.
- Duplicate page.
- Delete page.
- Reorder pages.
- Rename page.
- Select active page.

Acceptance criteria:

- Project can contain multiple pages.
- Export can export all pages or selected pages.

### Layers

Must support:

- Layer list.
- Rename layer.
- Reorder layer.
- Lock layer.
- Hide layer.
- Select layer from panel.

Acceptance criteria:

- Layer order matches canvas z-index.
- Hidden layer does not export.

### Templates

Must support:

- Template gallery.
- Filter by category.
- Search templates.
- Create project from template.
- Apply template to current project with confirmation.

Required starter templates:

- Instagram square post.
- YouTube thumbnail.
- Business flyer.
- Presentation title slide.
- Resume page.
- Event poster.

### Brand Kit

Must support:

- Create brand kit.
- Add brand colors.
- Add brand fonts.
- Add logo asset.
- Apply brand kit to project.
- Quick access brand colors in color picker.

## 9. UI Layout Requirements

The app must use a real editor layout:

- Top bar: project name, undo, redo, save status, export.
- Left sidebar: templates, elements, uploads, text, brand kit.
- Center: canvas/artboard.
- Right sidebar: object properties and layers.
- Bottom or side page strip for multi-page projects.

Responsive behavior:

- Desktop layout is primary.
- Tablet layout may collapse sidebars.
- Mobile layout should allow viewing and simple edits, but full editing can be limited.

Design style:

- Clean creative workspace.
- Professional controls.
- No large hero section.
- No decorative marketing layout.
- Use icons where appropriate.
- Text must not overflow buttons or panels.

## 10. Keyboard Shortcuts

Required:

- Ctrl/Cmd+S: save.
- Ctrl/Cmd+Z: undo.
- Ctrl/Cmd+Shift+Z or Ctrl/Cmd+Y: redo.
- Ctrl/Cmd+C: copy selected object.
- Ctrl/Cmd+V: paste.
- Ctrl/Cmd+D: duplicate.
- Delete/Backspace: delete selected object.
- Arrow keys: nudge.
- Shift+Arrow: larger nudge.
- Ctrl/Cmd+A: select all objects on active page.
- Ctrl/Cmd+G: group.
- Ctrl/Cmd+Shift+G: ungroup.
- Ctrl/Cmd+Plus: zoom in.
- Ctrl/Cmd+Minus: zoom out.

## 11. Undo and Redo

Requirements:

- Track editor operations in a history stack.
- Undo object add, delete, move, resize, rotate, text edit, style edit, page add, page delete.
- Redo undone operations.
- History should reset after project close unless persisted history is implemented.

Acceptance criteria:

- Moving an object can be undone.
- Text edits can be undone.
- Deleting an object can be undone.

## 12. Import and Export

### Export Formats

PNG:

- Export active page as PNG.
- Transparent background respected when selected.

JPEG:

- Export active page as JPEG.
- Background must be flattened.

PDF:

- Export all pages or selected pages.
- Preserve page order.

JSON:

- Export complete project state.
- Include schema version.
- Include assets when embedded.

### Import

Must support:

- Import project JSON.
- Validate schema version.
- Reject invalid object references.
- Report import errors clearly.

## 13. CLI Requirements

The app must include a CLI for testing and automation.

Required commands:

```bash
studioforge init
studioforge seed
studioforge project list
studioforge project create --name "Launch Poster" --size instagram-square
studioforge project export <project-id> --format json --out project.json
studioforge project import project.json
studioforge template list
studioforge asset list
studioforge doctor
```

CLI acceptance criteria:

- Commands return non-zero exit code on expected errors.
- `--json` output is valid JSON where supported.
- Missing project IDs show friendly errors.
- Export command writes a file.

## 14. Persistence Requirements

Required:

- Local database or local file store.
- Migrations or schema versioning.
- Auto-save project changes.
- Manual save command.
- Save status in UI.
- Project list persists across restarts.

Recommended tables:

- user_profiles
- design_projects
- design_pages
- canvas_objects
- assets
- templates
- brand_kits
- export_jobs
- project_history_events

## 15. Validation Rules

The app must reject:

- Empty project name.
- Invalid canvas dimensions.
- Unsupported export format.
- Invalid color values.
- Invalid JSON import.
- Canvas object without valid type.
- Asset without data or file path.
- Template with invalid project JSON.
- Deleting the last page.

## 16. Search Requirements

Must support:

- Search projects by name and description.
- Search templates by name, category, and tag.
- Search assets by name, type, and tag.
- Search layers by name.

## 17. Seed Data

Seed command must create:

- 1 default user profile.
- 3 brand kits.
- 12 templates.
- 20 built-in assets.
- 5 sample projects.
- At least 2 multi-page projects.

Seed behavior:

- Deterministic.
- Can run repeatedly without duplicates.
- Supports fresh reset with confirmation.

## 18. Testing Requirements

### Unit Tests

Must cover:

- Project validation.
- Canvas object validation.
- Layer z-index ordering.
- Undo/redo reducer.
- Page operations.
- Export JSON shape.
- Template import validation.
- Brand kit color validation.

### Integration Tests

Must cover:

- Create project.
- Add page.
- Add text object.
- Add shape object.
- Move object.
- Save project.
- Reopen project.
- Export JSON.
- Import JSON into new project.

### End-to-End Tests

Must cover:

- Editor loads.
- User creates new design from preset.
- User adds text.
- User changes text color.
- User adds shape.
- User reorders layers.
- User exports PNG or JSON.
- User creates project from template.

## 19. Performance Requirements

Must remain usable with:

- 100 projects.
- 50 pages in one project.
- 500 objects on one page.
- 500 uploaded assets.
- 100 templates.

Targets:

- Editor opens under 1 second after warm startup.
- Selecting object responds under 100 ms.
- Dragging object remains smooth.
- Saving project under 500 ms for common project sizes.
- Export active page under 3 seconds.

## 20. Accessibility Requirements

Must include:

- Keyboard focus states.
- Labels for controls.
- Buttons with accessible names.
- Sufficient contrast.
- Non-color-only status indicators.
- Reduced motion support.
- Logical tab order.

## 21. Security and Safety

Requirements:

- Uploaded files must be type-checked.
- JSON imports must be validated before applying.
- Local file paths must not be exposed in exported shared JSON unless explicitly requested.
- Dangerous HTML/script content in text objects must be escaped.
- SVG imports must be sanitized or disabled.

## 22. Milestones

### Milestone 1: Scaffold and Editor Shell

Deliver:

- App scaffold.
- CLI scaffold.
- Editor layout.
- Empty canvas.
- Project dashboard.
- Basic tests.

### Milestone 2: Canvas Objects

Deliver:

- Add text.
- Add shapes.
- Select, move, resize, rotate.
- Object properties panel.
- Layer panel.
- Undo/redo.

### Milestone 3: Persistence

Deliver:

- Local database or file store.
- Project save/load.
- Page save/load.
- Object save/load.
- Project history events.

### Milestone 4: Assets, Templates, and Brand Kits

Deliver:

- Upload images.
- Asset library.
- Template gallery.
- Starter templates.
- Brand kit editor.

### Milestone 5: Export and Import

Deliver:

- PNG export.
- JPEG export.
- PDF export.
- JSON export.
- JSON import.
- Export tests.

### Milestone 6: CLI and Automation

Deliver:

- Required CLI commands.
- Seed command.
- Doctor command.
- CLI tests.

### Milestone 7: Polish and Verification

Deliver:

- E2E tests.
- Accessibility pass.
- Performance pass.
- Documentation.
- Final verification report.

## 23. Definition of Done

The project is done when:

- User can create a design.
- User can add and edit text.
- User can add and edit shapes.
- User can upload and place images.
- User can manage pages.
- User can manage layers.
- User can save and reopen projects.
- User can use templates.
- User can use brand kits.
- User can export PNG, JPEG, PDF, and JSON.
- CLI can create, list, import, export, seed, and diagnose projects.
- Tests pass.
- README explains setup, usage, testing, import, export, and limitations.

## 24. Hard Mode Evaluation

Weak implementation:

- Static landing page.
- No real canvas editor.
- No object selection.
- No persistence.
- No export.
- No CLI.
- No tests.

Strong implementation:

- Real canvas object model.
- Smooth editor UI.
- Layer management.
- Undo and redo.
- Local project persistence.
- Template system.
- Brand kit system.
- PNG and JSON export.
- Meaningful tests.
- Clear implementation milestones.

