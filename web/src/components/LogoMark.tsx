/** 品牌标记：四角圆润的星形，用渐变区分于其它线性图标。 */
export default function LogoMark() {
  return (
    <svg className="logo-mark" width="30" height="30" viewBox="0 0 32 32" aria-hidden="true">
      <defs>
        <linearGradient id="logo-grad" x1="4" y1="2" x2="28" y2="30" gradientUnits="userSpaceOnUse">
          <stop offset="0%" stopColor="#4f8bff" />
          <stop offset="55%" stopColor="#3b6cf6" />
          <stop offset="100%" stopColor="#7a5cf0" />
        </linearGradient>
      </defs>
      <rect x="1.5" y="1.5" width="29" height="29" rx="9.5" fill="url(#logo-grad)" />
      <path
        d="M16 6.4c.34 4.5 1.9 6.06 6.4 6.4-4.5.34-6.06 1.9-6.4 6.4-.34-4.5-1.9-6.06-6.4-6.4 4.5-.34 6.06-1.9 6.4-6.4Z"
        fill="#ffffff"
        opacity="0.96"
      />
      <path
        d="M22.4 18.2c.17 2.25.95 3.03 3.2 3.2-2.25.17-3.03.95-3.2 3.2-.17-2.25-.95-3.03-3.2-3.2 2.25-.17 3.03-.95 3.2-3.2Z"
        fill="#ffffff"
        opacity="0.72"
      />
    </svg>
  );
}
