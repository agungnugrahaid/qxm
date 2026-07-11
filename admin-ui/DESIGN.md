---
name: Lumina Console
colors:
  surface: '#f8f9ff'
  surface-dim: '#d1daec'
  surface-bright: '#f8f9ff'
  surface-container-lowest: '#ffffff'
  surface-container-low: '#eff4ff'
  surface-container: '#e6eeff'
  surface-container-high: '#e0e9fa'
  surface-container-highest: '#dae3f4'
  on-surface: '#131c28'
  on-surface-variant: '#434751'
  inverse-surface: '#28313e'
  inverse-on-surface: '#ebf1ff'
  outline: '#737782'
  outline-variant: '#c3c6d2'
  surface-tint: '#345da2'
  primary: '#003472'
  on-primary: '#ffffff'
  primary-container: '#1e4b8f'
  on-primary-container: '#9dbdff'
  inverse-primary: '#acc7ff'
  secondary: '#00639b'
  on-secondary: '#ffffff'
  secondary-container: '#70bcff'
  on-secondary-container: '#004b77'
  tertiary: '#651d00'
  on-tertiary: '#ffffff'
  tertiary-container: '#882f0a'
  on-tertiary-container: '#ffa88a'
  error: '#ba1a1a'
  on-error: '#ffffff'
  error-container: '#ffdad6'
  on-error-container: '#93000a'
  primary-fixed: '#d7e2ff'
  primary-fixed-dim: '#acc7ff'
  on-primary-fixed: '#001a40'
  on-primary-fixed-variant: '#154589'
  secondary-fixed: '#cee5ff'
  secondary-fixed-dim: '#97cbff'
  on-secondary-fixed: '#001d33'
  on-secondary-fixed-variant: '#004a76'
  tertiary-fixed: '#ffdbcf'
  tertiary-fixed-dim: '#ffb59c'
  on-tertiary-fixed: '#390c00'
  on-tertiary-fixed-variant: '#812904'
  background: '#f8f9ff'
  on-background: '#131c28'
  surface-variant: '#dae3f4'
typography:
  headline-xl:
    fontFamily: Inter
    fontSize: 36px
    fontWeight: '700'
    lineHeight: 44px
    letterSpacing: -0.02em
  headline-lg:
    fontFamily: Inter
    fontSize: 28px
    fontWeight: '600'
    lineHeight: 36px
    letterSpacing: -0.01em
  headline-md:
    fontFamily: Inter
    fontSize: 22px
    fontWeight: '600'
    lineHeight: 28px
  body-lg:
    fontFamily: Inter
    fontSize: 18px
    fontWeight: '400'
    lineHeight: 28px
  body-md:
    fontFamily: Inter
    fontSize: 16px
    fontWeight: '400'
    lineHeight: 24px
  body-sm:
    fontFamily: Inter
    fontSize: 14px
    fontWeight: '400'
    lineHeight: 20px
  label-md:
    fontFamily: Inter
    fontSize: 12px
    fontWeight: '600'
    lineHeight: 16px
    letterSpacing: 0.05em
  mono-code:
    fontFamily: monospace
    fontSize: 14px
    fontWeight: '400'
    lineHeight: 20px
rounded:
  sm: 0.125rem
  DEFAULT: 0.25rem
  md: 0.375rem
  lg: 0.5rem
  xl: 0.75rem
  full: 9999px
spacing:
  base: 4px
  xs: 4px
  sm: 8px
  md: 16px
  lg: 24px
  xl: 40px
  gutter: 24px
  sidebar-width: 260px
---

## Brand & Style

The design system is engineered for high-performance administrative environments, combining the structural rigor of a developer console with a sophisticated, Earth-toned palette. The brand personality is **authoritative, precise, and dependable**, designed to reduce cognitive load during complex tasks while maintaining a premium aesthetic.

The visual style is **Corporate / Modern** with a focus on functional clarity. It utilizes a high-contrast foundation to ensure mission-critical data is immediately legible. The aesthetic avoids unnecessary ornamentation, favoring mathematical spacing, intentional hierarchy, and a blend of "tech" cool tones with "organic" warm highlights for status and alerting.

## Colors

The palette is derived from a sophisticated mix of atmospheric blues and terra-cotta tones. 

- **Primary & Sidebar:** A deep, authoritative navy (#1E4B8F) serves as the anchor for the sidebar and structural navigation, providing a focused, dark-themed frame for the content area.
- **Action Blue:** A vibrant, medium-bright blue (#4091D1) is reserved for primary actions, links, and active selection states to ensure high visibility.
- **Alerting & Status:** The terracotta (#D86941) is utilized for critical errors and high-priority destructive actions. The amber (#E39C42) is used for warnings and secondary highlights.
- **Surfaces:** Main content areas use a very light, cool-grey tint to reduce glare, while primary text maintains a high-contrast relationship with its background for accessibility compliance.

## Typography

This design system exclusively uses **Inter** to project a professional, systematic, and utilitarian feel. 

- **Hierarchy:** Use bold weights for headlines to create a clear scan pattern. Body text uses a standard 16px base for optimal legibility in data-dense views.
- **Labels:** Small labels and metadata use a slightly heavier weight and increased letter spacing to remain legible at small scales.
- **Monospace:** For console outputs or ID strings, use a system monospace font to differentiate machine-readable data from human-readable interface text.
- **Responsive:** Headlines scale down by roughly 20% on mobile devices to prevent excessive line wrapping in narrow containers.

## Layout & Spacing

The layout follows a **Fixed-Fluid Hybrid** model. The sidebar remains at a fixed 260px width, while the main content area utilizes a fluid 12-column grid with a maximum container width of 1440px to prevent excessive line lengths on ultra-wide monitors.

- **Rhythm:** A 4px baseline grid governs all spacing. Vertical margins between sections typically use `xl` (40px) or `lg` (24px) to maintain a sense of order.
- **Density:** The "Console" aesthetic requires moderate to high density. Use `sm` (8px) for internal component padding and `md` (16px) for standard grouping.
- **Mobile:** On mobile, the sidebar collapses into a hidden drawer, and margins reduce from 24px to 16px.

## Elevation & Depth

Visual hierarchy is established through **Tonal Layers** and **Low-contrast Outlines** rather than heavy shadows, preserving a modern "flat-plus" console feel.

- **Surface Tiers:** Backgrounds use the lightest grey-blue. Cards and content containers use pure white.
- **Borders:** Subtle 1px borders in a light neutral tint are the primary method of separation.
- **Depth:** Small, tight shadows (0px 2px 4px rgba(0,0,0,0.05)) are permitted only for floating elements like dropdowns, modals, or tooltips to indicate they exist on a superior Z-index.
- **Interactivity:** Elements slightly lift or change border color on hover to provide tactile feedback without breaking the systematic grid.

## Shapes

The design system uses **Soft** geometry. This slight rounding (4px - 8px) softens the professional rigidity of the console while maintaining a serious, structured appearance.

- **Standard:** Buttons, inputs, and small containers use 4px (`rounded-sm`).
- **Cards:** Main dashboard cards and large containers use 8px (`rounded-lg`).
- **Interactive Elements:** Checkboxes use a 2px radius for a sharp, precise look.

## Components

- **Buttons:** Primary buttons use the Action Blue (#4091D1) with white text. Critical buttons use Terracotta (#D86941). Secondary buttons use a ghost style with a subtle border.
- **Inputs:** Use white backgrounds with a 1px neutral border. Focus states must use a 2px Action Blue ring for accessibility.
- **Chips:** Used for status. "Success" uses a light blue tint with dark blue text; "Warning" uses Amber tint; "Error" uses Terracotta tint.
- **Sidebar:** Dark navy background with high-contrast white or light blue icons. Active nav items should have a vertical "indicator" bar in Action Blue.
- **Lists & Tables:** High-density rows with subtle zebra striping. Header rows use a slightly darker neutral tint and bold `label-md` typography.
- **Cards:** White background, 1px border, 8px corner radius. Used to group related metrics or configuration settings.