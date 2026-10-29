(function () {
  $(document).ready(function () {
    // Role-based access indicators
    $('.nav-link').each(function () {
      const $this = $(this);
      const href = $this.attr('href');
      if (!href || href === '#' || href.startsWith('javascript:')) return;

      const userRole = window.USER_ROLE || '';
      const allowedRoles = $this.data('roles');

      if (allowedRoles && typeof allowedRoles === 'string') {
        // Example: data-roles="ADMIN,TEACHER"
        const roles = allowedRoles.split(',').map(r => r.trim());
        if (!roles.includes(userRole)) {
          $this.parent('.nav-item').addClass('disabled');
          $this.append('<span class="badge role-badge badge-secondary">Restricted</span>');
        }
      }
    });

    // Auto-close alerts
    setTimeout(function () {
      $('.alert').alert('close');
    }, 5000);

    // Logout confirmation
    $('form[action*="logout"]').on('submit', function (e) {
      if (!confirm('Are you sure you want to logout?')) e.preventDefault();
    });



    // Notifications init if needed
    if (window.IS_AUTHENTICATED && window.NotificationManager) {
      window.notificationManager = new window.NotificationManager();
      $(window).on('beforeunload', function () {
        if (window.notificationManager) window.notificationManager.stopPolling();
      });
    }
  });


  window.getCSRFToken = window.getCSRFToken || function() {
    // your existing code
  };
})();
