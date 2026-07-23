(() => {
  const nav = document.getElementById("nav");
  const onScroll = () => nav.classList.toggle("is-scrolled", window.scrollY > 8);
  onScroll();
  window.addEventListener("scroll", onScroll, { passive: true });

  const revealTargets = document.querySelectorAll(".reveal, .how-step");
  const lineFill = document.querySelector(".how-line-fill");
  const observer = new IntersectionObserver((entries) => {
    entries.forEach((entry) => {
      if (!entry.isIntersecting) return;
      entry.target.classList.add("is-visible");
      observer.unobserve(entry.target);
      if (entry.target.closest(".how-steps") && lineFill) lineFill.classList.add("is-filled");
    });
  }, { threshold: .2, rootMargin: "0px 0px -60px 0px" });
  revealTargets.forEach((el) => observer.observe(el));
})();
