document.addEventListener('DOMContentLoaded', function() {
    const yearElement = document.getElementById('year');
    if (yearElement) {
        yearElement.textContent = new Date().getFullYear();
    }

    // Hamburger menu toggle functionality
    const menuToggle = document.querySelector(".menu-toggle");
    const navLinks = document.querySelector(".nav-links");

    if (menuToggle && navLinks) {
        menuToggle.addEventListener("click", function () {
            navLinks.classList.toggle("active"); // Toggle menu visibility
        });
    } else {
        console.error("Menu button or nav-links not found.");
    }

    const postsContainer = document.getElementById("substack-posts");
    if (postsContainer) {
        const allowedHost = "thinkingatcs.substack.com";

        fetch("data/substack-posts.json", { cache: "no-store" })
            .then(function (response) {
                if (!response.ok) {
                    throw new Error("Feed snapshot was unavailable.");
                }
                return response.json();
            })
            .then(function (feed) {
                if (!feed || !Array.isArray(feed.posts) || feed.posts.length !== 6) {
                    throw new Error("Feed snapshot had an unexpected format.");
                }

                const fragment = document.createDocumentFragment();
                feed.posts.forEach(function (post) {
                    const postUrl = new URL(post.url);
                    if (postUrl.protocol !== "https:" || postUrl.hostname !== allowedHost || !postUrl.pathname.startsWith("/p/")) {
                        throw new Error("Feed snapshot contained an unexpected link.");
                    }

                    const card = document.createElement("article");
                    card.className = "substack-post-card";

                    const time = document.createElement("time");
                    time.dateTime = String(post.date);
                    time.textContent = String(post.date_display);

                    const title = document.createElement("h3");
                    title.textContent = String(post.title);

                    const excerpt = document.createElement("p");
                    excerpt.textContent = String(post.excerpt);

                    const link = document.createElement("a");
                    link.href = postUrl.href;
                    link.target = "_blank";
                    link.rel = "noopener noreferrer";
                    link.textContent = "Read on Substack ↗";

                    card.append(time, title, excerpt, link);
                    fragment.appendChild(card);
                });

                postsContainer.replaceChildren(fragment);
            })
            .catch(function () {
                // Keep the checked-in cards visible if the generated snapshot is unavailable.
            });
    }

    const copyEmailButton = document.getElementById("copy-application-email");
    const applicationEmail = document.getElementById("application-email");
    const copyEmailStatus = document.getElementById("copy-email-status");

    if (copyEmailButton && applicationEmail && copyEmailStatus) {
        copyEmailButton.addEventListener("click", function () {
            const email = applicationEmail.value;

            function reportSuccess() {
                copyEmailStatus.textContent = "Email address copied.";
                copyEmailButton.textContent = "Copied";
                window.setTimeout(function () {
                    copyEmailButton.textContent = "Copy Email";
                }, 2200);
            }

            function selectForManualCopy() {
                applicationEmail.focus();
                applicationEmail.select();
                applicationEmail.setSelectionRange(0, email.length);
                copyEmailStatus.textContent = "The address is selected. Copy it with your keyboard.";
            }

            if (navigator.clipboard && window.isSecureContext) {
                navigator.clipboard.writeText(email).then(reportSuccess).catch(selectForManualCopy);
                return;
            }

            applicationEmail.focus();
            applicationEmail.select();
            applicationEmail.setSelectionRange(0, email.length);
            try {
                if (document.execCommand("copy")) {
                    reportSuccess();
                } else {
                    selectForManualCopy();
                }
            } catch (error) {
                selectForManualCopy();
            }
        });
    }
});
