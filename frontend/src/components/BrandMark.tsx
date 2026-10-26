import Image from "next/image";

// The app's logo / profile picture. To change it, replace public/brand/logo.png.
const LOGO = "/brand/logo.png";

/** The logo in a white tile; the image is scaled up to trim its white margin. */
export default function BrandMark({ size = 40, radius = 9999 }: { size?: number; radius?: number }) {
  return (
    <span
      style={{
        position: "relative",
        flex: "0 0 auto",
        width: size,
        height: size,
        borderRadius: radius,
        overflow: "hidden",
        background: "#ffffff",
        boxShadow: "0 0 0 2px rgba(255,255,255,.75), 0 6px 14px -8px rgba(30,20,10,.5)",
      }}
    >
      {/* Less zoom in a circle, so the logo's corners stay inside it. */}
      <Image src={LOGO} alt="" fill sizes={`${size}px`} priority style={{ objectFit: "cover", transform: `scale(${radius >= size / 2 ? 1.12 : 1.45})` }} />
    </span>
  );
}
