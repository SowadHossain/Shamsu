# Product Requirements Document: BiteFleet

## 1. Product Summary

BiteFleet is a Foodpanda-like food delivery marketplace application. It connects customers, restaurants, riders, and operations admins in one local-first prototype. The product must support browsing restaurants, managing menus, placing orders, tracking delivery, assigning riders, handling promotions, collecting reviews, managing support tickets, and viewing operational dashboards.

This PRD is intended to test Shamsu on a complex marketplace product. A weak implementation would only create a restaurant listing page. A strong implementation must model the full order lifecycle from customer checkout to restaurant preparation to rider delivery and customer review.

## 2. Goals

The app must allow:

- Customers to browse restaurants and menu items.
- Customers to search and filter restaurants by cuisine, rating, delivery fee, distance, and open status.
- Customers to add items to cart with modifiers.
- Customers to place orders with delivery address and payment method.
- Restaurants to accept, reject, prepare, and mark orders ready.
- Riders to receive assignments, pick up orders, and complete deliveries.
- Admins to monitor orders, riders, restaurants, refunds, support tickets, and platform metrics.
- Users to apply promo codes.
- Customers to track order status in real time or simulated real time.
- The system to store all data locally for testing.

## 3. Non-Goals

- No real payment gateway integration in version 1.
- No live GPS integration in version 1.
- No production authentication security.
- No actual SMS or push notifications.
- No real tax compliance.
- No real restaurant onboarding paperwork.
- No cloud deployment required for the first implementation.

## 4. Recommended Stack

If no stack exists:

- TypeScript
- React
- Vite
- Node.js
- SQLite
- Zod
- Vitest
- Playwright

The implementation may use another stack only if it preserves the local-first behavior, testability, and user workflows.

## 5. User Roles

### Entity: Customer

Fields:

- id: string, required, unique
- name: string, required
- email: string, required, unique, valid email
- phone: string, required
- default_address_id: string, optional, references Address
- loyalty_points: integer, default 0
- status: enum, values: active, blocked, deleted
- created_at: datetime
- updated_at: datetime
- deleted_at: datetime, nullable

Rules:

- Blocked customers cannot place orders.
- Email must be unique.
- Phone must be present for delivery contact.

### Entity: RestaurantUser

Fields:

- id: string, required, unique
- restaurant_id: string, required, references Restaurant
- name: string, required
- email: string, required, valid email
- role: enum, values: owner, manager, kitchen_staff
- active: boolean, default true
- created_at: datetime
- updated_at: datetime

Rules:

- Inactive restaurant users cannot update orders or menus.

### Entity: Rider

Fields:

- id: string, required, unique
- name: string, required
- phone: string, required
- vehicle_type: enum, values: bicycle, motorcycle, car, walking
- status: enum, values: offline, available, assigned, picking_up, delivering, paused, blocked
- current_zone_id: string, optional, references DeliveryZone
- current_lat: number, optional
- current_lng: number, optional
- rating: number, optional
- active: boolean, default true
- created_at: datetime
- updated_at: datetime

Rules:

- Only available riders can receive new assignments.
- Blocked riders cannot log in or accept assignments.

### Entity: AdminUser

Fields:

- id: string, required, unique
- name: string, required
- email: string, required, unique, valid email
- role: enum, values: admin, operations, support, finance
- active: boolean, default true
- created_at: datetime
- updated_at: datetime

Rules:

- Finance users can view payments/refunds but cannot change restaurant menus.
- Support users can manage support tickets and refunds under a configured limit.

## 6. Core Marketplace Entities

### Entity: Address

Fields:

- id: string, required, unique
- customer_id: string, required, references Customer
- label: string, optional
- line_1: string, required
- line_2: string, optional
- city: string, required
- area: string, required
- postal_code: string, optional
- lat: number, required
- lng: number, required
- delivery_instructions: text, optional
- created_at: datetime
- updated_at: datetime
- deleted_at: datetime, nullable

Rules:

- Customer must have at least one active address before placing a delivery order.

### Entity: DeliveryZone

Fields:

- id: string, required, unique
- name: string, required
- city: string, required
- polygon_json: text, optional
- base_delivery_fee: number, required
- surge_multiplier: number, default 1
- active: boolean, default true
- created_at: datetime
- updated_at: datetime

Rules:

- base_delivery_fee cannot be negative.
- surge_multiplier must be at least 1.
- Inactive zones cannot accept new orders.

### Entity: Restaurant

Fields:

- id: string, required, unique
- name: string, required
- slug: string, required, unique
- description: text, optional
- cuisine_types: string array
- phone: string, required
- email: string, optional, valid email
- address_line: string, required
- city: string, required
- area: string, required
- lat: number, required
- lng: number, required
- zone_id: string, required, references DeliveryZone
- status: enum, values: pending, active, paused, suspended, closed
- rating_average: number, default 0
- rating_count: integer, default 0
- min_order_amount: number, default 0
- base_prep_minutes: integer, default 20
- delivery_fee_override: number, optional
- cover_asset_id: string, optional, references Asset
- logo_asset_id: string, optional, references Asset
- created_at: datetime
- updated_at: datetime
- deleted_at: datetime, nullable

Rules:

- Only active restaurants appear in default customer browsing.
- Suspended restaurants cannot accept orders.
- min_order_amount cannot be negative.

### Entity: RestaurantHours

Fields:

- id: string, required, unique
- restaurant_id: string, required, references Restaurant
- day_of_week: integer, required, 0 through 6
- opens_at: time, required
- closes_at: time, required
- closed: boolean, default false

Rules:

- closes_at must be after opens_at unless closed is true.
- A restaurant is orderable only during open hours and active status.

### Entity: MenuCategory

Fields:

- id: string, required, unique
- restaurant_id: string, required, references Restaurant
- name: string, required
- description: text, optional
- sort_order: integer, required
- active: boolean, default true
- created_at: datetime
- updated_at: datetime

Rules:

- Categories are sorted by sort_order.
- Inactive categories are hidden from customers.

### Entity: MenuItem

Fields:

- id: string, required, unique
- restaurant_id: string, required, references Restaurant
- category_id: string, required, references MenuCategory
- name: string, required
- description: text, optional
- price: number, required
- discounted_price: number, optional
- image_asset_id: string, optional, references Asset
- available: boolean, default true
- vegetarian: boolean, default false
- spicy_level: integer, optional, 0 through 5
- preparation_minutes: integer, optional
- sort_order: integer, required
- created_at: datetime
- updated_at: datetime
- deleted_at: datetime, nullable

Rules:

- price must be greater than zero.
- discounted_price must be greater than zero and less than price when present.
- Unavailable items cannot be added to cart.

### Entity: ItemModifierGroup

Fields:

- id: string, required, unique
- menu_item_id: string, required, references MenuItem
- name: string, required
- min_selected: integer, default 0
- max_selected: integer, required
- required: boolean, default false
- sort_order: integer

Rules:

- max_selected must be at least 1.
- min_selected cannot exceed max_selected.
- Required groups must have min_selected greater than zero.

### Entity: ItemModifierOption

Fields:

- id: string, required, unique
- modifier_group_id: string, required, references ItemModifierGroup
- name: string, required
- price_delta: number, default 0
- available: boolean, default true
- sort_order: integer

Rules:

- Unavailable modifier options cannot be selected.

## 7. Cart and Order Entities

### Entity: Cart

Fields:

- id: string, required, unique
- customer_id: string, required, references Customer
- restaurant_id: string, optional, references Restaurant
- status: enum, values: active, checked_out, abandoned
- created_at: datetime
- updated_at: datetime

Rules:

- A cart can contain items from only one restaurant.
- Adding an item from another restaurant requires clearing the cart or starting a new cart.

### Entity: CartItem

Fields:

- id: string, required, unique
- cart_id: string, required, references Cart
- menu_item_id: string, required, references MenuItem
- quantity: integer, required
- selected_modifiers_json: text, optional
- unit_price_snapshot: number, required
- notes: text, optional
- created_at: datetime
- updated_at: datetime

Rules:

- Quantity must be greater than zero.
- Modifier selection must satisfy modifier group min/max rules.
- Price snapshot is stored so later menu changes do not affect cart pricing.

### Entity: Order

Fields:

- id: string, required, unique
- order_number: string, required, unique
- customer_id: string, required, references Customer
- restaurant_id: string, required, references Restaurant
- delivery_address_id: string, required, references Address
- assigned_rider_id: string, optional, references Rider
- status: enum, values: placed, restaurant_accepted, restaurant_rejected, preparing, ready_for_pickup, rider_assigned, picked_up, delivered, canceled, refunded
- subtotal: number, required
- delivery_fee: number, required
- service_fee: number, required
- discount_total: number, default 0
- total: number, required
- payment_method: enum, values: cash_on_delivery, card_mock, wallet_mock
- payment_status: enum, values: pending, authorized, paid, failed, refunded
- promo_code_id: string, optional, references PromoCode
- customer_note: text, optional
- estimated_delivery_minutes: integer, optional
- placed_at: datetime
- accepted_at: datetime, optional
- ready_at: datetime, optional
- picked_up_at: datetime, optional
- delivered_at: datetime, optional
- canceled_at: datetime, optional
- cancellation_reason: text, optional
- created_at: datetime
- updated_at: datetime

Rules:

- subtotal equals sum of order item line totals.
- total equals subtotal plus fees minus discounts.
- total cannot be negative.
- restaurant_rejected requires cancellation_reason.
- delivered requires picked_up_at.
- refunded requires payment_status refunded.

### Entity: OrderItem

Fields:

- id: string, required, unique
- order_id: string, required, references Order
- menu_item_id: string, required, references MenuItem
- name_snapshot: string, required
- quantity: integer, required
- unit_price_snapshot: number, required
- selected_modifiers_json: text, optional
- line_total: number, required
- notes: text, optional

Rules:

- Quantity must be positive.
- line_total must include modifiers and quantity.

### Entity: OrderStatusEvent

Fields:

- id: string, required, unique
- order_id: string, required, references Order
- actor_type: enum, values: customer, restaurant_user, rider, admin, system
- actor_id: string, optional
- from_status: string, optional
- to_status: string, required
- summary: string, required
- metadata_json: text, optional
- created_at: datetime

Rules:

- Every order status transition must create an event.
- Events are append-only.

## 8. Payments, Promotions, and Refunds

### Entity: PromoCode

Fields:

- id: string, required, unique
- code: string, required, unique
- description: text, optional
- discount_type: enum, values: percentage, fixed_amount, free_delivery
- discount_value: number, required
- min_order_amount: number, default 0
- max_discount_amount: number, optional
- starts_at: datetime, required
- ends_at: datetime, required
- usage_limit_total: integer, optional
- usage_limit_per_customer: integer, optional
- active: boolean, default true
- created_at: datetime
- updated_at: datetime

Rules:

- Code is case-insensitive.
- ends_at must be after starts_at.
- Promo cannot apply to orders below min_order_amount.
- Usage limits must be enforced.

### Entity: PaymentTransaction

Fields:

- id: string, required, unique
- order_id: string, required, references Order
- method: enum, values: cash_on_delivery, card_mock, wallet_mock
- status: enum, values: pending, authorized, captured, failed, refunded
- amount: number, required
- reference: string, optional
- created_at: datetime
- updated_at: datetime

Rules:

- Amount must be positive.
- Mock card payments can be authorized immediately.
- Cash payments remain pending until delivery.

### Entity: Refund

Fields:

- id: string, required, unique
- order_id: string, required, references Order
- requested_by_admin_id: string, required, references AdminUser
- amount: number, required
- reason: text, required
- status: enum, values: requested, approved, rejected, processed
- created_at: datetime
- updated_at: datetime

Rules:

- Refund amount cannot exceed paid order total.
- Refund requires reason.
- Processed refund updates payment_status.

## 9. Reviews, Support, and Notifications

### Entity: Review

Fields:

- id: string, required, unique
- order_id: string, required, references Order
- customer_id: string, required, references Customer
- restaurant_id: string, required, references Restaurant
- rider_id: string, optional, references Rider
- restaurant_rating: integer, required, 1 through 5
- rider_rating: integer, optional, 1 through 5
- comment: text, optional
- created_at: datetime
- updated_at: datetime

Rules:

- Only delivered orders can be reviewed.
- One review per order.
- Restaurant rating updates restaurant aggregate rating.

### Entity: SupportTicket

Fields:

- id: string, required, unique
- ticket_number: string, required, unique
- customer_id: string, optional, references Customer
- order_id: string, optional, references Order
- assigned_admin_id: string, optional, references AdminUser
- category: enum, values: late_delivery, missing_item, wrong_item, payment_issue, refund_request, account_issue, other
- status: enum, values: open, waiting_customer, waiting_restaurant, resolved, closed
- priority: enum, values: low, normal, high, urgent
- subject: string, required
- description: text, required
- resolution: text, optional
- created_at: datetime
- updated_at: datetime
- closed_at: datetime, optional

Rules:

- Resolution is required when status becomes resolved or closed.

### Entity: Notification

Fields:

- id: string, required, unique
- recipient_type: enum, values: customer, restaurant_user, rider, admin
- recipient_id: string, required
- channel: enum, values: in_app, email_mock, sms_mock
- title: string, required
- body: text, required
- read_at: datetime, optional
- created_at: datetime

Rules:

- Notifications are local/mock only in version 1.
- Order status changes should create in-app notifications.

### Entity: Asset

Fields:

- id: string, required, unique
- name: string, required
- asset_type: enum, values: restaurant_cover, restaurant_logo, menu_item_image, banner
- mime_type: string, required
- data_url: text, optional
- file_path: text, optional
- created_at: datetime
- updated_at: datetime

Rules:

- Asset must have data_url or file_path.
- Unsupported image types are rejected.

## 10. Required Customer UI

### View: Customer Home

Must show:

- Search bar.
- Current delivery address.
- Cuisine shortcuts.
- Promo banners.
- Featured restaurants.
- Popular near you.
- Recently ordered.

### View: Restaurant Search and Listing

Must support:

- Search by restaurant name.
- Search by cuisine.
- Filter by open now.
- Filter by rating.
- Filter by delivery fee.
- Sort by recommended, fastest delivery, rating, distance, delivery fee.

### View: Restaurant Detail

Must show:

- Restaurant cover image.
- Logo.
- Name.
- Rating.
- Delivery time estimate.
- Delivery fee.
- Minimum order amount.
- Menu categories.
- Menu items.
- Item modifier selection.

### View: Cart and Checkout

Must support:

- Cart item quantity updates.
- Modifier display.
- Item notes.
- Promo code entry.
- Delivery address selection.
- Payment method selection.
- Fee breakdown.
- Place order.

### View: Order Tracking

Must show:

- Current order status.
- Timeline.
- Restaurant status.
- Rider assignment.
- Estimated delivery time.
- Mock rider location progress if available.
- Cancel option when allowed.

### View: Customer Orders

Must show:

- Active orders.
- Past orders.
- Reorder action.
- Review action for delivered orders.
- Support action.

## 11. Required Restaurant Portal

### View: Restaurant Dashboard

Must show:

- Incoming orders.
- Preparing orders.
- Ready for pickup orders.
- Completed orders today.
- Revenue today.
- Average prep time.

### View: Order Management

Must support:

- Accept order.
- Reject order with reason.
- Mark preparing.
- Mark ready for pickup.
- View customer note.
- View item modifiers.

### View: Menu Management

Must support:

- Create category.
- Edit category.
- Create menu item.
- Edit price.
- Toggle item availability.
- Add modifier groups.
- Add modifier options.
- Upload image.

### View: Restaurant Settings

Must support:

- Edit restaurant description.
- Edit open hours.
- Pause restaurant.
- Resume restaurant.
- Set prep time.
- Set minimum order amount.

## 12. Required Rider App

### View: Rider Dashboard

Must show:

- Current status.
- Assigned order.
- Pickup restaurant.
- Delivery address.
- Earnings summary mock.

### View: Assignment Flow

Must support:

- Accept assignment.
- Reject assignment.
- Mark arrived at restaurant.
- Mark picked up.
- Mark delivered.
- Report issue.

Rules:

- Rider cannot mark delivered before picked up.
- Rider cannot accept another order while assigned unless batching is enabled.

## 13. Required Admin UI

### View: Admin Dashboard

Must show:

- Active orders.
- Delayed orders.
- Restaurant rejection rate.
- Rider availability.
- Revenue mock metrics.
- Refund requests.
- Open support tickets.

### View: Dispatch Board

Must support:

- View unassigned ready orders.
- View available riders.
- Assign rider manually.
- Reassign rider.
- See delayed pickup warnings.

### View: Restaurant Admin

Must support:

- Approve restaurant.
- Suspend restaurant.
- Pause restaurant.
- View menu.
- View order history.

### View: Support Desk

Must support:

- Ticket list.
- Ticket detail.
- Assign ticket.
- Change status.
- Add resolution.
- Create refund request from ticket.

## 14. Order Lifecycle Rules

Valid transitions:

- placed -> restaurant_accepted
- placed -> restaurant_rejected
- restaurant_accepted -> preparing
- preparing -> ready_for_pickup
- ready_for_pickup -> rider_assigned
- rider_assigned -> picked_up
- picked_up -> delivered
- placed -> canceled
- restaurant_accepted -> canceled
- delivered -> refunded

Invalid transitions must be rejected with clear errors.

Cancellation rules:

- Customer can cancel only before restaurant_accepted.
- Restaurant can reject only from placed.
- Admin can cancel before picked_up with reason.

Delivery delay rules:

- Order is delayed if current time exceeds estimated delivery time by more than 10 minutes.
- Delayed orders appear on admin dashboard.

## 15. Dispatch Rules

Rider assignment score should consider:

- Rider availability.
- Rider current zone.
- Distance to restaurant.
- Vehicle type.
- Rider rating.
- Number of active assignments.

Acceptance criteria:

- System can auto-assign best available rider.
- Admin can override assignment.
- If no rider is available, order remains ready_for_pickup and appears in dispatch board.

## 16. CLI Requirements

The app must include a CLI for testing.

Required commands:

```bash
bitefleet init
bitefleet seed
bitefleet doctor
bitefleet customer list
bitefleet restaurant list
bitefleet restaurant open <restaurant-id>
bitefleet restaurant pause <restaurant-id>
bitefleet menu item toggle <menu-item-id>
bitefleet order place --customer <customer-id> --restaurant <restaurant-id>
bitefleet order list --status placed
bitefleet order accept <order-id>
bitefleet order ready <order-id>
bitefleet rider list --status available
bitefleet rider assign --order <order-id> --rider <rider-id>
bitefleet order pickup <order-id>
bitefleet order deliver <order-id>
bitefleet promo apply --cart <cart-id> --code WELCOME10
bitefleet support ticket list
bitefleet export --out bitefleet-backup.json
bitefleet import bitefleet-backup.json
```

CLI acceptance criteria:

- Human-readable output by default.
- `--json` output where useful.
- Expected errors return non-zero exit code.
- Missing IDs produce friendly messages.

## 17. Persistence Requirements

Required:

- SQLite or equivalent local persistence.
- Migrations.
- Foreign key enforcement.
- Transactional checkout.
- Transactional order status changes.
- Transactional promo usage.
- Export/import with schema version.

Recommended tables:

- customers
- restaurant_users
- riders
- admin_users
- addresses
- delivery_zones
- restaurants
- restaurant_hours
- menu_categories
- menu_items
- item_modifier_groups
- item_modifier_options
- carts
- cart_items
- orders
- order_items
- order_status_events
- promo_codes
- payment_transactions
- refunds
- reviews
- support_tickets
- notifications
- assets

## 18. Validation Rules

Reject:

- Empty names.
- Invalid email.
- Negative prices.
- Negative delivery fees.
- Invalid order transition.
- Adding unavailable item to cart.
- Checkout from closed restaurant.
- Checkout below minimum order.
- Promo outside active date range.
- Promo exceeding usage limit.
- Rider assignment to unavailable rider.
- Delivered order without pickup.
- Review for non-delivered order.
- Refund over paid amount.

## 19. Seed Data

Seed command must create:

- 20 customers.
- 25 restaurants.
- 10 delivery zones.
- 20 riders.
- 5 admin users.
- 12 cuisine types.
- 150 menu items.
- 50 modifier groups.
- 150 modifier options.
- 40 active carts.
- 80 orders across multiple statuses.
- 10 promo codes.
- 20 reviews.
- 15 support tickets.
- 100 notifications.

Seed behavior:

- Deterministic.
- Can run repeatedly without duplicates.
- Supports `--fresh` with confirmation.

## 20. Testing Requirements

### Unit Tests

Must cover:

- Restaurant open/closed calculation.
- Cart one-restaurant rule.
- Modifier validation.
- Order total calculation.
- Promo discount calculation.
- Order transition validation.
- Rider assignment scoring.
- Refund amount validation.
- Review eligibility.

### Integration Tests

Must cover:

- Customer creates cart and checks out.
- Restaurant accepts and prepares order.
- Rider is assigned, picks up, and delivers.
- Promo code usage limit is enforced.
- Restaurant rejects order.
- Admin manually reassigns rider.
- Delivered order can be reviewed.
- Support ticket can create refund request.
- Export/import round trip.

### End-to-End Tests

Must cover:

- Customer home loads seeded restaurants.
- Customer searches restaurant.
- Customer opens restaurant detail.
- Customer adds modified item to cart.
- Customer places order.
- Restaurant accepts order.
- Rider completes delivery.
- Admin dashboard shows active and delayed orders.

## 21. Performance Requirements

Must remain usable with:

- 10,000 restaurants.
- 500,000 menu items.
- 100,000 customers.
- 100,000 orders.
- 5,000 riders.

Prototype targets:

- Restaurant listing loads under 800 ms after warm startup.
- Menu detail loads under 500 ms for common menus.
- Checkout completes under 1 second.
- Admin dashboard loads under 1 second.
- CLI order list returns under 1 second for filtered queries.

## 22. Accessibility Requirements

Required:

- Keyboard accessible navigation.
- Visible focus states.
- Form labels.
- Error messages tied to inputs.
- Sufficient contrast.
- No color-only status indicators.
- Screen-reader-friendly order status timeline.

## 23. Security and Safety

Required:

- Validate all JSON imports.
- Sanitize customer notes and restaurant descriptions.
- Do not expose local file paths in exports unless explicitly requested.
- Prevent status changes by unauthorized role.
- Record admin actions.

## 24. Analytics and Dashboards

Admin analytics must include:

- Gross order value.
- Average order value.
- Orders by status.
- Orders by restaurant.
- Rider completion count.
- Average delivery time.
- Restaurant rejection rate.
- Promo usage.
- Refund total.

## 25. Milestones

### Milestone 1: Scaffold and Data Model

Deliver:

- App scaffold.
- CLI scaffold.
- SQLite schema.
- Seed command.
- Basic customer restaurant list.
- Basic tests.

### Milestone 2: Customer Ordering

Deliver:

- Customer home.
- Restaurant listing.
- Restaurant detail.
- Cart.
- Checkout.
- Order creation.

### Milestone 3: Restaurant Portal

Deliver:

- Restaurant dashboard.
- Order accept/reject.
- Preparing and ready statuses.
- Menu management.
- Restaurant settings.

### Milestone 4: Rider and Dispatch

Deliver:

- Rider dashboard.
- Assignment logic.
- Pickup and delivery flow.
- Admin dispatch board.

### Milestone 5: Promotions, Payments, Reviews, Support

Deliver:

- Promo code engine.
- Mock payment transactions.
- Reviews.
- Support tickets.
- Refund requests.

### Milestone 6: Admin and Analytics

Deliver:

- Admin dashboard.
- Restaurant admin.
- Rider admin.
- Support desk.
- Analytics.

### Milestone 7: Import, Export, Polish

Deliver:

- Export/import.
- E2E tests.
- Accessibility pass.
- Performance indexes.
- Documentation.

## 26. Definition of Done

Done means:

- Customer can browse restaurants.
- Customer can place an order.
- Restaurant can accept and prepare order.
- Rider can pick up and deliver order.
- Admin can monitor and dispatch.
- Promo codes work.
- Reviews work.
- Support tickets work.
- Data persists locally.
- CLI supports core workflows.
- Tests pass.
- README documents setup, seed, run, test, import, and export.

## 27. Hard Mode Evaluation

Weak implementation:

- Static restaurant cards only.
- No cart.
- No checkout.
- No order lifecycle.
- No restaurant portal.
- No rider dispatch.
- No persistence.
- No tests.

Strong implementation:

- Full local marketplace data model.
- Real cart and checkout flow.
- Order status transitions enforced.
- Restaurant and rider workflows.
- Admin dispatch board.
- Promo and refund rules.
- Deterministic seed data.
- CLI automation.
- Meaningful tests.

