/**
 * ServerTable - Lightweight Server-Side Pagination and Search Client
 * Integrates directly with our FastAPI /api/ endpoints.
 */

class ServerTable {
  constructor(options) {
    this.table = document.querySelector(options.tableSelector);
    if (!this.table) return;

    this.apiUrl = options.apiUrl;
    this.rowRenderer = options.rowRenderer;
    this.headers = options.headers || []; // mapping of th index to API sort key

    this.page = 1;
    this.perPage = 25;
    this.search = "";
    this.sortCol = options.defaultSortCol || "";
    this.sortDir = options.defaultSortDir || "asc";
    this.debounceTimer = null;

    this.tbody = this.table.querySelector("tbody") || this.table.appendChild(document.createElement("tbody"));
    this.thead = this.table.querySelector("thead");

    this.initLayout();
    this.bindEvents();
    this.load();
  }

  initLayout() {
    // Wrap the table in simple-datatables structure to preserve CSS styling
    const wrapper = document.createElement("div");
    wrapper.className = "datatable-wrapper sortable searchable";
    this.table.parentNode.insertBefore(wrapper, this.table);

    // Create top controls
    const topBar = document.createElement("div");
    topBar.className = "datatable-top";
    topBar.innerHTML = `
      <div class="datatable-dropdown">
        <label>
          <select class="datatable-selector">
            <option value="10">10</option>
            <option value="25" selected>25</option>
            <option value="50">50</option>
            <option value="100">100</option>
          </select> entries per page
        </label>
      </div>
      <div class="datatable-search">
        <input class="datatable-input" placeholder="Search..." type="search" aria-label="Search table">
      </div>
    `;
    wrapper.appendChild(topBar);

    // Create main container
    const container = document.createElement("div");
    container.className = "datatable-container";
    wrapper.appendChild(container);
    container.appendChild(this.table);

    // Create bottom controls
    const bottomBar = document.createElement("div");
    bottomBar.className = "datatable-bottom";
    bottomBar.innerHTML = `
      <div class="datatable-info">Showing 0 to 0 of 0 entries</div>
      <nav class="datatable-pagination">
        <ul class="datatable-pagination-list" style="display:flex; list-style:none; padding:0; margin:0;"></ul>
      </nav>
    `;
    wrapper.appendChild(bottomBar);

    this.wrapper = wrapper;
    this.infoDiv = bottomBar.querySelector(".datatable-info");
    this.paginationList = bottomBar.querySelector(".datatable-pagination-list");
    this.searchField = topBar.querySelector(".datatable-input");
    this.perPageSelector = topBar.querySelector(".datatable-selector");
  }

  bindEvents() {
    // Search input (with 300ms debounce)
    this.searchField.addEventListener("input", (e) => {
      clearTimeout(this.debounceTimer);
      this.debounceTimer = setTimeout(() => {
        this.search = e.target.value;
        this.page = 1;
        this.load();
      }, 300);
    });

    // Entries per page dropdown
    this.perPageSelector.addEventListener("change", (e) => {
      this.perPage = parseInt(e.target.value, 10);
      this.page = 1;
      this.load();
    });

    // Sorting headers
    if (this.thead) {
      const ths = this.thead.querySelectorAll("th");
      ths.forEach((th, index) => {
        const sortKey = this.headers[index];
        if (!sortKey) return; // not sortable

        th.style.cursor = "pointer";
        th.classList.add("sortable");
        
        // Add styling indicator wrapper if not present
        if (!th.querySelector("button")) {
          const content = th.innerHTML;
          th.innerHTML = `<button class="datatable-sorter" style="background:none; border:none; color:inherit; font:inherit; padding:0; display:inline-flex; align-items:center; gap:0.35rem; cursor:pointer;">
            <span>${content}</span>
            <i class="sort-icon" style="opacity:0.3; font-style:normal; font-size:0.7rem; display:inline-block;">&#9650;&#9660;</i>
          </button>`;
        }

        th.addEventListener("click", () => {
          if (this.sortCol === sortKey) {
            this.sortDir = this.sortDir === "asc" ? "desc" : "asc";
          } else {
            this.sortCol = sortKey;
            this.sortDir = "asc";
          }
          
          this.updateHeaderStyles();
          this.load();
        });
      });

      // Set initial sorting header styles
      this.updateHeaderStyles();
    }
  }

  updateHeaderStyles() {
    if (!this.thead) return;
    const ths = this.thead.querySelectorAll("th");
    ths.forEach((th, index) => {
      const sortKey = this.headers[index];
      if (!sortKey) return;
      
      const icon = th.querySelector(".sort-icon");
      if (this.sortCol === sortKey) {
        th.classList.add("active-sort");
        if (icon) {
          icon.innerHTML = this.sortDir === "asc" ? "&#9650;" : "&#9660;";
        }
      } else {
        th.classList.remove("active-sort");
        if (icon) {
          icon.innerHTML = "&#9650;&#9660;";
        }
      }
    });
  }

  load() {
    this.tbody.innerHTML = `<tr><td colspan="10" style="text-align:center; padding:2rem; color:var(--on-surface-variant);">
      <span class="loading-spinner">Loading data...</span>
    </td></tr>`;

    const params = new URLSearchParams({
      search: this.search,
      page: this.page,
      per_page: this.perPage,
      sort_col: this.sortCol,
      sort_dir: this.sortDir
    });

    fetch(`${this.apiUrl}?${params.toString()}`)
      .parseJson = fetch(`${this.apiUrl}?${params.toString()}`)
      .then(res => res.json())
      .then(result => {
        this.render(result.data, result.total);
      })
      .catch(err => {
        console.error("Failed to load table data", err);
        this.tbody.innerHTML = `<tr><td colspan="10" style="text-align:center; padding:2rem; color:var(--terracotta);">
          Failed to load database records.
        </td></tr>`;
      });
  }

  render(data, total) {
    this.tbody.innerHTML = "";
    if (data.length === 0) {
      this.tbody.innerHTML = `<tr><td colspan="10" style="text-align:center; padding:2rem; color:var(--on-surface-variant);">
        No matching records found.
      </td></tr>`;
      this.updatePagination(0, 0, 0);
      return;
    }

    data.forEach((row, index) => {
      const tr = document.createElement("tr");
      tr.innerHTML = this.rowRenderer(row, index);
      this.tbody.appendChild(tr);
    });

    // Re-initialize any Lucide icons created in the rows
    if (window.lucide) {
      window.lucide.createIcons();
    }

    const start = (this.page - 1) * this.perPage + 1;
    const end = Math.min(this.page * this.perPage, total);
    this.updatePagination(start, end, total);
  }

  updatePagination(start, end, total) {
    this.infoDiv.textContent = `Showing ${start} to ${end} of ${total} entries`;
    this.paginationList.innerHTML = "";

    const totalPages = Math.ceil(total / this.perPage);
    if (totalPages <= 1) return;

    // Previous Button
    const prevLi = document.createElement("li");
    prevLi.innerHTML = `<a class="${this.page === 1 ? 'disabled' : ''}" style="cursor:pointer; user-select:none;">‹</a>`;
    if (this.page > 1) {
      prevLi.addEventListener("click", () => { this.page--; this.load(); });
    }
    this.paginationList.appendChild(prevLi);

    // Page Buttons
    const maxVisible = 5;
    let startPage = Math.max(1, this.page - 2);
    let endPage = Math.min(totalPages, startPage + maxVisible - 1);
    if (endPage - startPage < maxVisible - 1) {
      startPage = Math.max(1, endPage - maxVisible + 1);
    }

    for (let i = startPage; i <= endPage; i++) {
      const li = document.createElement("li");
      li.className = i === this.page ? "active" : "";
      li.innerHTML = `<a style="cursor:pointer; user-select:none;">${i}</a>`;
      li.addEventListener("click", () => {
        if (this.page !== i) {
          this.page = i;
          this.load();
        }
      });
      this.paginationList.appendChild(li);
    }

    // Next Button
    const nextLi = document.createElement("li");
    nextLi.innerHTML = `<a class="${this.page === totalPages ? 'disabled' : ''}" style="cursor:pointer; user-select:none;">›</a>`;
    if (this.page < totalPages) {
      nextLi.addEventListener("click", () => { this.page++; this.load(); });
    }
    this.paginationList.appendChild(nextLi);
  }
}

window.ServerTable = ServerTable;
